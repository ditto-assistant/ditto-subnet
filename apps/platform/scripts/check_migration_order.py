#!/usr/bin/env python3
"""Validate that Alembic migrations remain a safe, linear history.

Two modes:

``check_migration_order.py [base_ref] [head_ref]``
    The CI mode. Validates *head_ref* -- the working tree by default --
    against ``base_ref`` (default ``origin/main``): nothing removed,
    nothing edited, dated forward, and the **merge result** resolving to
    exactly one head.

    That last one is the whole point, and it is deliberately asserted
    against ``base_ref + head_ref`` rather than against the branch alone.
    A branch cut before a second migration landed on ``main`` is perfectly
    linear on its own; the divergence exists only in the merge, which is
    the thing that gets deployed. Passing ``head_ref`` explicitly lets the
    check be re-run for an open PR from any checkout -- notably from
    ``main`` after it moves, which is when a green PR silently goes stale.

``check_migration_order.py --head``
    The deploy mode. Resolves the *working tree's* migrations to a single
    head and prints it, using only the standard library -- no venv, no
    alembic import, no database. ``scripts/update.sh`` runs this before it
    touches the host: a last line of defence for anything that reached
    ``main`` without passing the merge-result check above (a bypass, a
    direct push, a check that was never required).

Both failure paths name every head, the file each lives in, and the two
ways to reconcile them. Alembic's own error carries none of that.
"""

from __future__ import annotations

import ast
import re
import subprocess
import sys
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

MIGRATIONS_DIR = Path("alembic/versions")
MIGRATION_NAME = re.compile(r"^(?P<date>\d{4}_\d{2}_\d{2})_.+\.py$")


@dataclass(frozen=True)
class Migration:
    path: str
    revision: str
    down_revision: str | tuple[str, ...] | None


def _parents(migration: Migration) -> tuple[str, ...]:
    if migration.down_revision is None:
        return ()
    if isinstance(migration.down_revision, str):
        return (migration.down_revision,)
    return migration.down_revision


class MigrationError(ValueError):
    """Raised when a migration history violates repository policy."""


def _git(*args: str) -> str:
    return subprocess.run(
        ["git", *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout


def _assignment(tree: ast.Module, name: str, path: str) -> object:
    for node in tree.body:
        if (
            isinstance(node, ast.AnnAssign)
            and isinstance(node.target, ast.Name)
            and node.target.id == name
            and node.value is not None
        ):
            return ast.literal_eval(node.value)
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == name
            for target in node.targets
        ):
            return ast.literal_eval(node.value)
    raise MigrationError(f"{path}: missing {name}")


def parse_migration(path: str, source: str) -> Migration:
    """Parse the revision relationship without importing migration code."""
    try:
        tree = ast.parse(source, filename=path)
        revision = _assignment(tree, "revision", path)
        down_revision = _assignment(tree, "down_revision", path)
    except (SyntaxError, ValueError) as exc:
        raise MigrationError(f"{path}: cannot parse migration metadata: {exc}") from exc

    if not isinstance(revision, str) or not revision:
        raise MigrationError(f"{path}: revision must be a non-empty string")
    if isinstance(down_revision, tuple):
        if not down_revision or not all(
            isinstance(parent, str) and parent for parent in down_revision
        ):
            raise MigrationError(
                f"{path}: down_revision must contain non-empty revision strings"
            )
        if len(set(down_revision)) != len(down_revision):
            raise MigrationError(f"{path}: down_revision contains duplicates")
    elif down_revision is not None and not isinstance(down_revision, str):
        raise MigrationError(
            f"{path}: down_revision must be a revision string or tuple of strings"
        )
    return Migration(path=path, revision=revision, down_revision=down_revision)


def _history_heads(migrations: list[Migration], label: str) -> set[str]:
    """Return every head after validating a connected, acyclic migration DAG."""
    by_revision: dict[str, Migration] = {}
    children: defaultdict[str, list[str]] = defaultdict(list)
    roots: list[str] = []

    for migration in migrations:
        previous = by_revision.get(migration.revision)
        if previous is not None:
            raise MigrationError(
                f"{label}: duplicate revision {migration.revision}: "
                f"{previous.path}, {migration.path}"
            )
        by_revision[migration.revision] = migration

    for migration in migrations:
        parents = _parents(migration)
        if not parents:
            roots.append(migration.revision)
            continue
        for parent in parents:
            if parent not in by_revision:
                raise MigrationError(
                    f"{migration.path}: unknown down_revision {parent}"
                )
            children[parent].append(migration.revision)

    if len(roots) != 1:
        raise MigrationError(f"{label}: expected one root revision, found {len(roots)}")

    visited: set[str] = set()
    ready = list(roots)
    remaining_parents = {
        revision: len(_parents(migration))
        for revision, migration in by_revision.items()
    }
    while ready:
        current = ready.pop()
        visited.add(current)
        for child in children.get(current, []):
            remaining_parents[child] -= 1
            if remaining_parents[child] == 0:
                ready.append(child)

    if len(visited) != len(migrations):
        raise MigrationError(f"{label}: migration history has a cycle")
    return set(by_revision) - set(children)


def validate_linear_history(migrations: list[Migration], label: str) -> str:
    """Return the sole head after validating a resolved migration DAG."""
    heads = _history_heads(migrations, label)
    if len(heads) != 1:
        raise MigrationError(
            f"{label}: expected one head revision, found {len(heads)}: "
            + ", ".join(sorted(heads))
        )
    return next(iter(heads))


def _paths_at(ref: str) -> list[str]:
    output = _git("ls-tree", "-r", "--name-only", ref, "--", str(MIGRATIONS_DIR))
    return sorted(path for path in output.splitlines() if path.endswith(".py"))


def _migrations_at(ref: str) -> list[Migration]:
    return [
        parse_migration(path, _git("show", f"{ref}:{path}")) for path in _paths_at(ref)
    ]


def _head_migrations() -> list[Migration]:
    paths = sorted(MIGRATIONS_DIR.glob("*.py"))
    return [parse_migration(str(path), path.read_text()) for path in paths]


def _merge_base(base_ref: str, head_ref: str) -> str:
    return _git("merge-base", base_ref, head_ref).strip()


def _paths_for(ref: str | None) -> set[str]:
    """Migration paths at *ref*, or in the working tree when it is ``None``."""
    if ref is None:
        return {str(path) for path in MIGRATIONS_DIR.glob("*.py")}
    return set(_paths_at(ref))


def _migrations_for(ref: str | None) -> list[Migration]:
    return _head_migrations() if ref is None else _migrations_at(ref)


def merge_result(base: list[Migration], head: list[Migration]) -> list[Migration]:
    """The ``alembic/versions`` content that merging *head* into *base* yields.

    Migrations are immutable and none may be deleted -- :func:`check` asserts
    both against the merge base first -- so the merge is exactly the union of
    the two file sets. That means the merge result can be resolved without
    performing the merge, from any checkout, which is what lets this run
    against a PR branch that was never rebased.
    """
    by_path = {migration.path: migration for migration in head}
    by_path.update({migration.path: migration for migration in base})
    return [by_path[path] for path in sorted(by_path)]


def _head_table(
    heads: list[str],
    by_revision: dict[str, Migration],
    base_heads: frozenset[str] | set[str] | None = None,
    base_ref: str | None = None,
) -> str:
    """One line per head: the revision, the file it lives in, and its origin."""
    rows = []
    for revision in heads:
        migration = by_revision.get(revision)
        row = f"    {revision}  {migration.path if migration else '<unknown file>'}"
        if base_heads is not None:
            row += (
                f"  (already a head on {base_ref})"
                if revision in base_heads
                else "  (added by this branch)"
            )
        rows.append(row)
    return "\n".join(rows)


def _remedy(
    heads: list[str], base_ref: str = "origin/main", base_head: str | None = None
) -> str:
    """Why two heads break everything, and the two ways to reconcile them."""
    rebase = (
        f"  * rebase onto current {base_ref} and repoint down_revision at its "
        f"head {base_head}"
        if base_head is not None
        else f"  * rebase onto current {base_ref} and repoint down_revision at its head"
    )
    return "\n".join(
        [
            "Alembic linears by down_revision, not by merge date, so two "
            "branches that each extend the same parent stay divergent however "
            "git merges them. `alembic upgrade head` then refuses to run with "
            "\"Multiple head revisions are present for given argument 'head'\", "
            "which fails every migration -- the deploy and the whole DB test "
            "tier with it.",
            "Reconcile on this branch, before merging, either way:",
            f"{rebase} (renumbering the YYYY_MM_DD_ filename too if it now "
            "precedes the newest migration there), or",
            f'  * uv run alembic merge -m "merge heads" {" ".join(heads)}',
            "Review both branches for conflicting changes to the same table "
            "before assuming an empty merge revision is correct.",
        ]
    )


def require_single_merged_head(
    merged: list[Migration], base_ref: str, base_heads: set[str]
) -> str:
    """Return the merge result's sole head, or explain the divergence.

    This is the assertion the 2026-07-28 outage needed. Validating a branch on
    its own passes a PR that was cut before a second migration landed on
    *base_ref*: both PRs are individually linear and the second head exists
    only in the merge -- which is the thing that actually gets deployed.
    """
    heads = sorted(_history_heads(merged, f"{base_ref} + this branch"))
    if len(heads) == 1:
        return heads[0]

    by_revision = {migration.revision: migration for migration in merged}
    message = [
        f"merging this branch into {base_ref} would leave {len(heads)} "
        "Alembic heads, not one:",
        _head_table(heads, by_revision, base_heads=base_heads, base_ref=base_ref),
    ]
    if all(revision in base_heads for revision in heads):
        message.append(
            f"Every head above is already on {base_ref}, so this branch did "
            "not cause the divergence -- but it does not reconcile it either, "
            f"and {base_ref} stays undeployable until something does."
        )
    message.append(
        _remedy(
            heads,
            base_ref=base_ref,
            base_head=next(iter(base_heads)) if len(base_heads) == 1 else None,
        )
    )
    raise MigrationError("\n".join(message))


def check(base_ref: str, head_ref: str | None = None) -> tuple[int, str, str]:
    """Validate *head_ref* -- the working tree by default -- against *base_ref*.

    Nothing removed, nothing edited, dated forward, and -- the assertion that
    matters -- the *merge result* resolves to exactly one head.
    """
    label = head_ref or "HEAD"
    base_paths = set(_paths_at(base_ref))
    head_paths = _paths_for(head_ref)

    # Against the merge base, not the base tip: a migration that landed on
    # `base_ref` after this branch was cut is missing from the branch without
    # the branch having removed anything.
    ancestor_paths = set(_paths_at(_merge_base(base_ref, label)))
    removed = sorted(ancestor_paths - head_paths)
    if removed:
        raise MigrationError("existing migrations were removed: " + ", ".join(removed))

    changed = _git(
        "diff",
        "--name-only",
        "--diff-filter=M",
        f"{base_ref}...{label}",
        "--",
        str(MIGRATIONS_DIR),
    ).splitlines()
    if changed:
        raise MigrationError("existing migrations are immutable: " + ", ".join(changed))

    base_migrations = _migrations_at(base_ref)
    head_migrations = _migrations_for(head_ref)
    base_heads = _history_heads(base_migrations, base_ref)
    merged_head = require_single_merged_head(
        merge_result(base_migrations, head_migrations), base_ref, base_heads
    )

    new_paths = sorted(head_paths - base_paths)
    base_dates = [MIGRATION_NAME.match(Path(path).name) for path in base_paths]
    if any(match is None for match in base_dates):
        raise MigrationError(
            f"{base_ref}: migration filename does not use YYYY_MM_DD_name.py"
        )
    latest_base_date = max(
        match.group("date") for match in base_dates if match is not None
    )

    for path in new_paths:
        match = MIGRATION_NAME.match(Path(path).name)
        if match is None:
            raise MigrationError(
                f"{path}: migration filename must use YYYY_MM_DD_name.py"
            )
        if match.group("date") < latest_base_date:
            raise MigrationError(
                f"{path}: date precedes {base_ref}'s latest migration date "
                f"{latest_base_date}"
            )

    # "new migrations extend the base head" needs no separate assertion: a new
    # migration that chains off anything else is a second head, and
    # require_single_merged_head has already rejected it by name.
    return len(new_paths), ", ".join(sorted(base_heads)), merged_head


def resolve_working_tree_head() -> str:
    """Return the sole head of the checked-out migrations.

    Raises :class:`MigrationError` naming every head, plus the exact
    ``alembic merge`` that reconciles them, when there is more than one.
    """
    migrations = _head_migrations()
    heads = sorted(_history_heads(migrations, "working tree"))
    if len(heads) == 1:
        return heads[0]
    by_revision = {migration.revision: migration for migration in migrations}
    raise MigrationError(
        f"{len(heads)} head revisions are present: {', '.join(heads)}.\n"
        + _head_table(heads, by_revision)
        + "\n"
        + _remedy(heads)
    )


def head_mode() -> int:
    """``--head``: print the single working-tree head, or explain the divergence."""
    try:
        head = resolve_working_tree_head()
    except MigrationError as exc:
        print(f"migration-head: {exc}", file=sys.stderr)
        return 1
    print(head)
    return 0


def main() -> int:
    if len(sys.argv) > 1 and sys.argv[1] == "--head":
        return head_mode()
    base_ref = sys.argv[1] if len(sys.argv) > 1 else "origin/main"
    head_ref = sys.argv[2] if len(sys.argv) > 2 else None
    try:
        count, base_head, head = check(base_ref, head_ref)
    except (MigrationError, subprocess.CalledProcessError) as exc:
        print(f"migration-order: {exc}", file=sys.stderr)
        return 1
    print(
        f"migration-order: ok ({count} new migration(s); "
        f"{base_ref} head {base_head}; merged head {head})"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
