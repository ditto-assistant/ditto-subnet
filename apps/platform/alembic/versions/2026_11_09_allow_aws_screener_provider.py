"""Allow an on-demand AWS EC2 screener node as the enrolled primary provider.

``aws`` joins every screener provider check: enrolled nodes, their bootstrap
grants and capacity events, and the build, source-review, and trusted-build rows
that record the claiming node's provider.
"""

from alembic import op

revision = "e1a7c3b9d250"
down_revision = "c4d8e2f1a937"
branch_labels = None
depends_on = None

_NODE_TABLES = {
    "screener_nodes": "screener_nodes_provider_check",
    "screener_node_bootstrap_grants": "screener_node_bootstrap_grants_provider_check",
}
_EVENT_TABLE = "screener_capacity_events"
_WORK_TABLES = (
    "trusted_image_builds",
    "submission_image_builds",
    "submission_source_reviews",
)


def _replace(table: str, name: str, condition: str) -> None:
    op.drop_constraint(name, table, type_="check")
    op.create_check_constraint(name, table, condition)


def _apply(node: str, work: str) -> None:
    for table, name in _NODE_TABLES.items():
        _replace(table, name, f"provider IN ({node})")
    _replace(
        _EVENT_TABLE,
        f"{_EVENT_TABLE}_provider_check",
        f"provider IS NULL OR provider IN ({node})",
    )
    for table in _WORK_TABLES:
        _replace(
            table,
            f"{table}_provider_check",
            f"provider IS NULL OR provider IN ({work})",
        )


def upgrade() -> None:
    _apply(
        "'gcp', 'targon', 'hetzner', 'aws', 'home', 'test'",
        "'targon', 'gcp', 'hetzner', 'aws'",
    )


def downgrade() -> None:
    _apply(
        "'gcp', 'targon', 'hetzner', 'home', 'test'",
        "'targon', 'gcp', 'hetzner'",
    )
