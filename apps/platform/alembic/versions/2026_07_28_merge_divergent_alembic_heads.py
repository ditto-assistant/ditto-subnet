"""Merge the two divergent Alembic heads on main.

`main` currently has two heads, so every migration run fails with "Multiple head
revisions are present for given argument 'head'". That takes the whole database
test tier and the deploy down with it: CI is red on `main` itself at deead2d,
and the Deploy job failed at d3ffc730.

The two heads:

* ``f4b7d2c91ae5`` -- ``2026_07_27_add_never_disclose_release_policy`` (#505),
  whose ``down_revision`` is ``b2e9d4a17c60``.
* ``c7a4f1e2b903`` -- ``2026_07_27_reinstate_an_evicted_submission``, whose
  ``down_revision`` is ``a7c14f8bd260``.

Both were written against different tips and merged without either being
rebased onto the other, which is the ordinary way this happens: Alembic linears
by ``down_revision``, not by merge date, so nothing about the git merge forced
them into a single chain.

This revision is an empty merge point. It has no ``upgrade``/``downgrade`` body
on purpose -- neither branch's schema changes are altered, re-run, or reordered
by it. It only rejoins the chain so `head` is once again singular. The two
branches touch unrelated tables (release policy vs. submission queue reinstate),
so the order in which they land carries no schema meaning.
"""

from collections.abc import Sequence

revision: str = "45ef71514f21"
down_revision: str | Sequence[str] | None = ("f4b7d2c91ae5", "c7a4f1e2b903")
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """No-op: this revision only rejoins two divergent branches."""


def downgrade() -> None:
    """No-op: splitting the chain again is what the merge exists to undo."""
