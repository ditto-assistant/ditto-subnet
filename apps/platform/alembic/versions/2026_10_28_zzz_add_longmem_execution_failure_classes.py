"""admit the scorer's LongMemEval executor and coordinator-step failure classes

Revision ID: 9d1e5c7a3f28
Revises: 6b4f8a2c91d3
Create Date: 2026-10-28

``6b4f8a2c91d3`` admitted the executor-stage classes. Measured in production
the same night on bundle ``2ede5da4``: the stage arrived as
``longmem_exec_dimension_execution`` -- correct, and one call wrapping roughly
sixty failure points, none of which recorded its cause, even on a validator we
operate. These classes name the point: the coordinator step
(``longmem_dim_*``, LongMemEval first then both ablations) and, inside
LongMemEval's executor, the phase and the provider-accounting check that failed
(``longmem_eval_*``).

Generated from the Go vocabulary, not transcribed. A Go test reads the shared
Python allowlist and fails on any difference with what the binary can emit, and
``test_failure_class_constraint_matches_the_shared_allowlist`` holds this
constraint to that allowlist. Written out rather than imported for the reason
the earlier revisions give: a migration is a snapshot.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "9d1e5c7a3f28"
down_revision: str | Sequence[str] | None = "6b4f8a2c91d3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_TABLE = "confirmation_bundle_tickets"
_CLASS_CONSTRAINT = "confirmation_tickets_failure_class_check"

_REPORTER_CLASS_VALUES = (
    "sandbox_oom",
    "lease_revoked",
    "validator_infrastructure",
    "platform_infrastructure",
    "dittobench",
    "platform",
    "validator",
    "evidence_schema",
    "timeout",
    "transport",
    "unclassified",
)
_HARNESS_CLASS_VALUES = (
    "longmem_run_http_status",
    "longmem_run_malformed_json",
    "longmem_run_missing_final_text",
    "longmem_run_request",
    "longmem_run_response_read",
    "longmem_run_response_too_large",
    "longmem_run_tool_endpoint",
    "longmem_seed_http_status",
    "longmem_seed_incomplete_ack",
    "longmem_seed_malformed_json",
    "longmem_seed_request",
    "longmem_seed_response_read",
    "longmem_seed_response_too_large",
)
_EXECUTOR_CLASS_VALUES = (
    "longmem_exec_ablation_dataset",
    "longmem_exec_broker_session",
    "longmem_exec_dimension_execution",
    "longmem_exec_embedding_phase",
    "longmem_exec_execution",
    "longmem_exec_harness_health",
    "longmem_exec_installation_validation",
    "longmem_exec_longmem_dataset",
    "longmem_exec_provider_capabilities",
    "longmem_exec_provider_session",
    "longmem_exec_runtime_acquire",
    "longmem_exec_runtime_close",
    "longmem_exec_runtime_material",
    "longmem_exec_runtime_validation",
    "longmem_exec_sandbox_availability",
    "longmem_exec_sandbox_start",
    "longmem_exec_sandbox_stop",
    "longmem_exec_screened_image",
    "longmem_exec_source_validation",
)
_NEW_CLASS_VALUES = (
    "longmem_dim_ablation_construct",
    "longmem_dim_ablation_coordinate",
    "longmem_dim_ablation_evaluate",
    "longmem_dim_ablation_marshal",
    "longmem_dim_ablation_responder",
    "longmem_dim_ablation_upstream",
    "longmem_dim_budget_unfundable",
    "longmem_dim_deadline_missing",
    "longmem_dim_longmem_digest",
    "longmem_dim_longmem_execute",
    "longmem_dim_longmem_marshal",
    "longmem_dim_longmem_validate",
    "longmem_dim_mode_inactive",
    "longmem_dim_wire_digest",
    "longmem_eval_accounting_final",
    "longmem_eval_accounting_final_cost_source",
    "longmem_eval_accounting_final_fallback_used",
    "longmem_eval_accounting_final_identity_drift",
    "longmem_eval_accounting_final_mixed_zero_lanes",
    "longmem_eval_accounting_final_provider_evidence",
    "longmem_eval_accounting_final_request_free_accounting",
    "longmem_eval_accounting_initial",
    "longmem_eval_accounting_initial_cost_source",
    "longmem_eval_accounting_initial_fallback_used",
    "longmem_eval_accounting_initial_identity_drift",
    "longmem_eval_accounting_initial_lane_coverage",
    "longmem_eval_accounting_initial_meter_unavailable",
    "longmem_eval_accounting_initial_nonzero_start",
    "longmem_eval_accounting_initial_provider_evidence",
    "longmem_eval_accounting_initial_request_free_accounting",
    "longmem_eval_accounting_judge",
    "longmem_eval_accounting_judge_cost_source",
    "longmem_eval_accounting_judge_fallback_used",
    "longmem_eval_accounting_judge_identity_drift",
    "longmem_eval_accounting_judge_lane_coverage",
    "longmem_eval_accounting_judge_meter_unavailable",
    "longmem_eval_accounting_judge_provider_evidence",
    "longmem_eval_accounting_judge_receipt_changed",
    "longmem_eval_accounting_judge_receipt_uncommitted",
    "longmem_eval_accounting_judge_regressed",
    "longmem_eval_accounting_judge_request_free_accounting",
    "longmem_eval_accounting_run",
    "longmem_eval_accounting_run_cost_source",
    "longmem_eval_accounting_run_fallback_used",
    "longmem_eval_accounting_run_identity_drift",
    "longmem_eval_accounting_run_lane_coverage",
    "longmem_eval_accounting_run_meter_unavailable",
    "longmem_eval_accounting_run_provider_evidence",
    "longmem_eval_accounting_run_receipt_changed",
    "longmem_eval_accounting_run_receipt_uncommitted",
    "longmem_eval_accounting_run_regressed",
    "longmem_eval_accounting_run_request_free_accounting",
    "longmem_eval_accounting_seed",
    "longmem_eval_accounting_seed_cost_source",
    "longmem_eval_accounting_seed_fallback_used",
    "longmem_eval_accounting_seed_identity_drift",
    "longmem_eval_accounting_seed_lane_coverage",
    "longmem_eval_accounting_seed_meter_unavailable",
    "longmem_eval_accounting_seed_provider_evidence",
    "longmem_eval_accounting_seed_receipt_changed",
    "longmem_eval_accounting_seed_receipt_uncommitted",
    "longmem_eval_accounting_seed_regressed",
    "longmem_eval_accounting_seed_request_free_accounting",
    "longmem_eval_aggregate",
    "longmem_eval_dataset",
    "longmem_eval_evidence",
    "longmem_eval_judge_budget",
    "longmem_eval_judge_opaque",
    "longmem_eval_projection",
    "longmem_eval_reader_budget",
    "longmem_eval_run_opaque",
    "longmem_eval_run_unreceipted",
    "longmem_eval_seed_incomplete",
    "longmem_eval_seed_opaque",
    "longmem_eval_setup",
    "longmem_eval_time_budget",
)


def _class_expression(values: Sequence[str]) -> str:
    rendered = ", ".join(f"'{value}'" for value in values)
    return f"failure_class IS NULL OR failure_class IN ({rendered})"


def upgrade() -> None:
    op.drop_constraint(_CLASS_CONSTRAINT, _TABLE, type_="check")
    op.create_check_constraint(
        _CLASS_CONSTRAINT,
        _TABLE,
        _class_expression(
            (
                *_REPORTER_CLASS_VALUES,
                *_HARNESS_CLASS_VALUES,
                *_EXECUTOR_CLASS_VALUES,
                *_NEW_CLASS_VALUES,
            )
        ),
    )


def downgrade() -> None:
    # Failed tickets are immutable signed history; fold the finer classes back
    # to the coarse one rather than stranding rows or failing the downgrade.
    op.execute(
        sa.text(
            f"UPDATE {_TABLE} SET failure_class = 'dittobench' "  # noqa: S608
            "WHERE failure_class LIKE 'longmem\\_eval\\_%' "
            "OR failure_class LIKE 'longmem\\_dim\\_%'"
        )
    )
    op.drop_constraint(_CLASS_CONSTRAINT, _TABLE, type_="check")
    op.create_check_constraint(
        _CLASS_CONSTRAINT,
        _TABLE,
        _class_expression(
            (*_REPORTER_CLASS_VALUES, *_HARNESS_CLASS_VALUES, *_EXECUTOR_CLASS_VALUES)
        ),
    )
