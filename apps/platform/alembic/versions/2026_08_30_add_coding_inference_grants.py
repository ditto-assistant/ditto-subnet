"""add ticket-bound shadow coding inference grants

Revision ID: 7a1e3c9b5d42
Revises: c6d9f2a14b83
Create Date: 2026-08-30 00:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "7a1e3c9b5d42"
down_revision: str | Sequence[str] | None = "c6d9f2a14b83"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "coding_inference_grants",
        sa.Column("grant_id", sa.UUID(), nullable=False),
        sa.Column("ticket_id", sa.UUID(), nullable=False),
        sa.Column("run_row_id", sa.UUID(), nullable=False),
        sa.Column("task_count", sa.Integer(), nullable=False),
        sa.Column("validator_hotkey", sa.Text(), nullable=False),
        sa.Column("case_id", sa.Text(), nullable=False),
        sa.Column("profile_capability_id", sa.Text(), nullable=False),
        sa.Column("inference_grant_sha256", sa.Text(), nullable=False),
        sa.Column("model", sa.Text(), nullable=False),
        sa.Column("provider_api", sa.Text(), nullable=False),
        sa.Column("provider_route", sa.Text(), nullable=False),
        sa.Column("receipt_provider", sa.Text(), nullable=False),
        sa.Column("provider_route_profile", sa.Text(), nullable=False),
        sa.Column("provider_account_guardrail", sa.Text(), nullable=False),
        sa.Column("provider_pipeline_policy", sa.Text(), nullable=False),
        sa.Column("provider_cache_policy", sa.Text(), nullable=False),
        sa.Column("reasoning_effort", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("bearer_digest", sa.Text(), nullable=True),
        sa.Column("broker_public_key", sa.Text(), nullable=True),
        sa.Column("generation", sa.Integer(), nullable=False),
        sa.Column("request_budget", sa.Integer(), nullable=False),
        sa.Column("prompt_token_budget", sa.BigInteger(), nullable=False),
        sa.Column("completion_token_budget", sa.BigInteger(), nullable=False),
        sa.Column("cost_budget_usd_micros", sa.BigInteger(), nullable=False),
        sa.Column(
            "request_count", sa.Integer(), server_default=sa.text("0"), nullable=False
        ),
        sa.Column(
            "prompt_tokens",
            sa.BigInteger(),
            server_default=sa.text("0"),
            nullable=False,
        ),
        sa.Column(
            "completion_tokens",
            sa.BigInteger(),
            server_default=sa.text("0"),
            nullable=False,
        ),
        sa.Column(
            "cost_usd_micros",
            sa.BigInteger(),
            server_default=sa.text("0"),
            nullable=False,
        ),
        sa.Column(
            "active_requests",
            sa.Integer(),
            server_default=sa.text("0"),
            nullable=False,
        ),
        sa.Column("expires_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("weight_eligible", sa.Boolean(), nullable=False),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "task_count = 1 AND generation BETWEEN 0 AND 2147483647 "
            "AND request_budget BETWEEN 1 AND 256 "
            "AND prompt_token_budget BETWEEN 1 AND 2000000 "
            "AND completion_token_budget BETWEEN 1 AND 250000 "
            "AND cost_budget_usd_micros BETWEEN 1 AND 100000000",
            name="coding_inference_grants_authority_bounds_check",
        ),
        sa.CheckConstraint(
            "request_count BETWEEN 0 AND request_budget "
            "AND prompt_tokens >= 0 "
            "AND completion_tokens >= 0 "
            "AND cost_usd_micros >= 0 "
            "AND active_requests BETWEEN 0 AND 1",
            name="coding_inference_grants_accounting_check",
        ),
        sa.CheckConstraint(
            "inference_grant_sha256 ~ '^[0-9a-f]{64}$' "
            "AND (bearer_digest IS NULL OR bearer_digest ~ '^[0-9a-f]{64}$') "
            "AND (broker_public_key IS NULL OR "
            "broker_public_key ~ '^[A-Za-z0-9_-]{43}$')",
            name="coding_inference_grants_crypto_check",
        ),
        sa.CheckConstraint(
            "model = 'openai/gpt-5.6-luna' "
            "AND provider_api = 'openrouter' "
            "AND provider_account_guardrail = 'openrouter_private_account_v1' "
            "AND provider_pipeline_policy = 'no_plugins_no_transforms_v1' "
            "AND provider_cache_policy = 'disabled_v1' "
            "AND reasoning_effort = 'medium'",
            name="coding_inference_grants_locked_route_check",
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'active', 'revoked', 'exhausted') "
            "AND ((status = 'pending' AND generation = 0 "
            "AND bearer_digest IS NULL AND broker_public_key IS NULL) "
            "OR (status = 'active' AND generation > 0 "
            "AND bearer_digest IS NOT NULL AND broker_public_key IS NOT NULL) "
            "OR (status IN ('revoked', 'exhausted') "
            "AND bearer_digest IS NULL AND broker_public_key IS NULL)) "
            "AND ((status = 'revoked') = (revoked_at IS NOT NULL))",
            name="coding_inference_grants_state_check",
        ),
        sa.CheckConstraint(
            "octet_length(case_id) BETWEEN 1 AND 256 "
            "AND octet_length(profile_capability_id) BETWEEN 1 AND 256 "
            "AND octet_length(provider_route) BETWEEN 1 AND 128 "
            "AND octet_length(receipt_provider) BETWEEN 1 AND 128 "
            "AND octet_length(provider_route_profile) BETWEEN 1 AND 128 "
            "AND case_id !~ '[[:space:][:cntrl:]]' "
            "AND profile_capability_id !~ '[[:space:][:cntrl:]]'",
            name="coding_inference_grants_identifiers_check",
        ),
        sa.CheckConstraint(
            "expires_at > created_at AND weight_eligible = false",
            name="coding_inference_grants_shadow_check",
        ),
        sa.ForeignKeyConstraint(
            ["ticket_id", "run_row_id", "task_count"],
            [
                "coding_shadow_tickets.ticket_id",
                "coding_shadow_tickets.run_row_id",
                "coding_shadow_tickets.task_count",
            ],
            name="coding_inference_grants_ticket_fkey",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint(
            "grant_id",
            name="coding_inference_grants_pkey",
        ),
        sa.UniqueConstraint(
            "ticket_id",
            name="coding_inference_grants_ticket_key",
        ),
        sa.UniqueConstraint(
            "grant_id",
            "ticket_id",
            name="coding_inference_grants_grant_ticket_key",
        ),
    )
    op.create_index(
        "coding_inference_grants_validator_expiry_idx",
        "coding_inference_grants",
        ["validator_hotkey", "expires_at"],
        unique=False,
        postgresql_where=sa.text("status IN ('pending', 'active')"),
    )


def downgrade() -> None:
    op.drop_index(
        "coding_inference_grants_validator_expiry_idx",
        table_name="coding_inference_grants",
    )
    op.drop_table("coding_inference_grants")
