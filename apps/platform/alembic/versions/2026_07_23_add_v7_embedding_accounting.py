"""add ticket scoped v7 embedding accounting

Revision ID: f4a8c21e7d60
Revises: e2b7c91d4a60
Create Date: 2026-07-23
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "f4a8c21e7d60"
down_revision: str | None = "e2b7c91d4a60"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "inference_grants",
        sa.Column(
            "embedding_model",
            sa.Text(),
            server_default="perplexity/pplx-embed-v1-0.6b",
            nullable=False,
        ),
    )
    op.add_column(
        "inference_grants",
        sa.Column(
            "embedding_profile",
            sa.Text(),
            server_default="dittobench-v7-openrouter-pplx-embed-v1-0.6b-768-v1",
            nullable=False,
        ),
    )
    op.add_column(
        "inference_grants",
        sa.Column(
            "embedding_provider",
            sa.Text(),
            server_default="Perplexity",
            nullable=False,
        ),
    )
    op.add_column(
        "inference_grants",
        sa.Column(
            "embedding_dimensions", sa.Integer(), server_default="768", nullable=False
        ),
    )
    op.add_column(
        "inference_grants",
        sa.Column(
            "embedding_request_budget",
            sa.Integer(),
            server_default="100000",
            nullable=False,
        ),
    )
    op.add_column(
        "inference_grants",
        sa.Column(
            "embedding_token_budget",
            sa.BigInteger(),
            server_default="1000000000",
            nullable=False,
        ),
    )
    op.add_column(
        "inference_grants",
        sa.Column(
            "embedding_request_count", sa.Integer(), server_default="0", nullable=False
        ),
    )
    op.add_column(
        "inference_grants",
        sa.Column(
            "embedding_tokens", sa.BigInteger(), server_default="0", nullable=False
        ),
    )
    op.add_column(
        "inference_grants",
        sa.Column(
            "embedding_cost_microusd",
            sa.BigInteger(),
            server_default="0",
            nullable=False,
        ),
    )
    op.add_column(
        "inference_grants",
        sa.Column(
            "embedding_active_requests",
            sa.Integer(),
            server_default="0",
            nullable=False,
        ),
    )
    op.create_check_constraint(
        "inference_grants_embedding_request_budget",
        "inference_grants",
        "embedding_request_budget > 0",
    )
    op.create_check_constraint(
        "inference_grants_embedding_token_budget",
        "inference_grants",
        "embedding_token_budget > 0",
    )
    op.create_check_constraint(
        "inference_grants_embedding_dimensions",
        "inference_grants",
        "embedding_dimensions = 768",
    )
    op.create_check_constraint(
        "inference_grants_embedding_active_requests",
        "inference_grants",
        "embedding_active_requests >= 0",
    )
    op.add_column(
        "inference_requests",
        sa.Column("request_kind", sa.Text(), server_default="chat", nullable=False),
    )
    op.create_check_constraint(
        "inference_requests_kind",
        "inference_requests",
        "request_kind IN ('chat', 'embedding')",
    )
    op.create_index(
        "inference_requests_kind_started_idx",
        "inference_requests",
        ["request_kind", "started_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "inference_requests_kind_started_idx", table_name="inference_requests"
    )
    op.drop_constraint("inference_requests_kind", "inference_requests", type_="check")
    op.drop_column("inference_requests", "request_kind")
    op.drop_constraint(
        "inference_grants_embedding_active_requests", "inference_grants", type_="check"
    )
    op.drop_constraint(
        "inference_grants_embedding_dimensions", "inference_grants", type_="check"
    )
    op.drop_constraint(
        "inference_grants_embedding_token_budget", "inference_grants", type_="check"
    )
    op.drop_constraint(
        "inference_grants_embedding_request_budget", "inference_grants", type_="check"
    )
    for column in (
        "embedding_active_requests",
        "embedding_cost_microusd",
        "embedding_tokens",
        "embedding_request_count",
        "embedding_token_budget",
        "embedding_request_budget",
        "embedding_dimensions",
        "embedding_provider",
        "embedding_profile",
        "embedding_model",
    ):
        op.drop_column("inference_grants", column)
