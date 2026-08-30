from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from scripts.check_migration_order import (
    Migration,
    MigrationError,
    merge_result,
    parse_migration,
    require_single_merged_head,
    resolve_working_tree_head,
    validate_linear_history,
)

ROOT = Path(__file__).parents[3]

MIGRATION = """\
revision: str = "{revision}"
down_revision: str | None = {down!r}
"""


def migration(revision: str, down_revision: str | None) -> Migration:
    return Migration(f"{revision}.py", revision, down_revision)


def test_linear_history_returns_single_head() -> None:
    migrations = [migration("one", None), migration("two", "one")]

    assert validate_linear_history(migrations, "test") == "two"


def test_duplicate_revision_is_rejected() -> None:
    migrations = [migration("one", None), migration("one", None)]

    with pytest.raises(MigrationError, match="duplicate revision one"):
        validate_linear_history(migrations, "test")


def test_parallel_heads_are_rejected() -> None:
    migrations = [
        migration("one", None),
        migration("two", "one"),
        migration("three", "one"),
    ]

    with pytest.raises(MigrationError, match="expected one head revision"):
        validate_linear_history(migrations, "test")


def test_merge_revision_resolves_parallel_heads() -> None:
    source = """
revision: str = "merge"
down_revision: tuple[str, str] = ("one", "two")
"""
    merge = parse_migration("merge.py", source)
    migrations = [
        migration("root", None),
        migration("one", "root"),
        migration("two", "root"),
        merge,
    ]

    assert validate_linear_history(migrations, "test") == "merge"


def test_merge_revision_rejects_duplicate_parents() -> None:
    source = """
revision: str = "merge"
down_revision: tuple[str, str] = ("one", "one")
"""

    with pytest.raises(MigrationError, match="down_revision contains duplicates"):
        parse_migration("merge.py", source)


def test_repository_migration_history_has_one_head() -> None:
    migrations = [
        parse_migration(str(path), path.read_text())
        for path in sorted(Path("alembic/versions").glob("*.py"))
    ]

    assert validate_linear_history(migrations, "repository")


def test_working_tree_head_resolves_for_this_repository() -> None:
    """The deploy preflight has to agree with the CI check on this checkout."""
    assert resolve_working_tree_head()


def _run_head_mode(cwd: Path) -> subprocess.CompletedProcess[str]:
    """Run the deploy-side `--head` mode the way update.sh does.

    Deliberately the system interpreter with no venv and no database: the
    preflight must be able to run before `uv sync`.
    """
    return subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "check_migration_order.py"), "--head"],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
    )


def _write_history(tmp_path: Path, *, diverged: bool) -> None:
    versions = tmp_path / "alembic" / "versions"
    versions.mkdir(parents=True)
    (versions / "2026_07_01_root.py").write_text(
        MIGRATION.format(revision="root", down=None)
    )
    (versions / "2026_07_02_first.py").write_text(
        MIGRATION.format(revision="e7b4c02a5d18", down="root")
    )
    if diverged:
        (versions / "2026_07_02_second.py").write_text(
            MIGRATION.format(revision="e5b8c31d47af", down="root")
        )


def test_head_mode_prints_the_single_head(tmp_path: Path) -> None:
    _write_history(tmp_path, diverged=False)

    result = _run_head_mode(tmp_path)

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "e7b4c02a5d18"


def test_head_mode_names_every_head_and_the_merge_that_fixes_it(
    tmp_path: Path,
) -> None:
    """Two migrations extended the same parent and both landed.

    By the time the deploy sees this the damage is done -- the pre-merge
    guard below is what is supposed to stop it -- but `update.sh` still has
    to explain itself. The message has to carry the revisions, the files and
    the remedy, because alembic's own error carries none of them.
    """
    _write_history(tmp_path, diverged=True)

    result = _run_head_mode(tmp_path)

    assert result.returncode == 1
    assert "2 head revisions are present" in result.stderr
    assert "e5b8c31d47af" in result.stderr
    assert "e7b4c02a5d18" in result.stderr
    assert "2026_07_02_first.py" in result.stderr
    assert "2026_07_02_second.py" in result.stderr
    assert (
        'uv run alembic merge -m "merge heads" e5b8c31d47af e7b4c02a5d18'
        in result.stderr
    )


# --- the merge result -------------------------------------------------------
#
# The 2026-07-28 outage in one sentence: two PRs that were each individually
# linear against the `main` they were cut from produced a second head once
# both had merged, because Alembic linears by down_revision and not by merge
# date. Everything below is about catching that *before* the merge.


def test_merge_result_is_the_union_of_both_file_sets() -> None:
    base = [migration("root", None), migration("landed", "root")]
    head = [migration("root", None), migration("mine", "root")]

    merged = merge_result(base, head)

    assert [m.revision for m in merged] == ["landed", "mine", "root"]


def test_merge_result_keeps_the_base_copy_of_a_shared_path() -> None:
    """Migrations are immutable, so the base's copy is the one that survives."""
    base = [Migration("shared.py", "base-revision", None)]
    head = [Migration("shared.py", "head-revision", None)]

    assert merge_result(base, head) == [Migration("shared.py", "base-revision", None)]


def test_single_merged_head_returns_the_head_when_the_branch_extends_base() -> None:
    base = [migration("root", None)]
    head = [migration("root", None), migration("mine", "root")]

    merged = merge_result(base, head)

    assert require_single_merged_head(merged, "origin/main", {"root"}) == "mine"


def test_merged_heads_name_both_revisions_their_files_and_both_remedies() -> None:
    """The exact 2026-07-28 fork, with the revisions and files it really had."""
    base = [
        Migration(
            "alembic/versions/2026_07_27_record_evicted.py", "b2e9d4a17c60", None
        ),
        Migration(
            "alembic/versions/2026_07_27_add_ticket_failure_detail.py",
            "a7c14f8bd260",
            "b2e9d4a17c60",
        ),
        Migration(
            "alembic/versions/2026_07_27_reinstate_an_evicted_submission.py",
            "c7a4f1e2b903",
            "a7c14f8bd260",
        ),
    ]
    head = [
        base[0],
        Migration(
            "alembic/versions/2026_07_27_add_never_disclose_release_policy.py",
            "f4b7d2c91ae5",
            "b2e9d4a17c60",
        ),
    ]

    with pytest.raises(MigrationError) as excinfo:
        require_single_merged_head(
            merge_result(base, head), "origin/main", {"c7a4f1e2b903"}
        )

    message = str(excinfo.value)
    assert "would leave 2 Alembic heads" in message
    # Both offending revisions, each named with the file it lives in.
    assert "c7a4f1e2b903  alembic/versions/2026_07_27_reinstate_an_evicted" in message
    assert "f4b7d2c91ae5  alembic/versions/2026_07_27_add_never_disclose" in message
    # And which side each came from, so the author knows what to rebase onto.
    assert "already a head on origin/main" in message
    assert "added by this branch" in message
    # Both remedies.
    assert "rebase onto current origin/main" in message
    assert 'uv run alembic merge -m "merge heads" c7a4f1e2b903 f4b7d2c91ae5' in message


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout


def _commit(repo: Path, name: str, revision: str, down: str | None) -> None:
    path = repo / "alembic" / "versions" / name
    path.write_text(MIGRATION.format(revision=revision, down=down))
    _git(repo, "add", str(path))
    _git(repo, "commit", "-m", f"add {revision}")


def _run_check(
    repo: Path, base_ref: str, head_ref: str
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts" / "check_migration_order.py"),
            base_ref,
            head_ref,
        ],
        cwd=repo,
        capture_output=True,
        text=True,
        check=False,
    )


@pytest.fixture
def stale_branch_repo(tmp_path: Path) -> Path:
    """The 2026-07-28 fork, rebuilt as two real branches.

    `main` is at b2e9d4a17c60 when `never-disclose` is cut from it. Two more
    migrations then land on `main` (#553, then #524). Neither branch is ever
    rebased, and both are individually linear.
    """
    repo = tmp_path / "repo"
    (repo / "alembic" / "versions").mkdir(parents=True)
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "test")

    _commit(repo, "2026_07_26_add_artifact_fetch_audit.py", "e8b3c05d7a41", None)
    _commit(repo, "2026_07_27_record_evicted_leases.py", "b2e9d4a17c60", "e8b3c05d7a41")

    # The feature branch is cut while b2e9d4a17c60 is still the head of main.
    _git(repo, "switch", "-q", "-c", "never-disclose")
    _commit(
        repo,
        "2026_07_27_add_never_disclose_release_policy.py",
        "f4b7d2c91ae5",
        "b2e9d4a17c60",
    )

    # Meanwhile main moves on, twice, and nobody rebases #505 onto it.
    _git(repo, "switch", "-q", "main")
    _commit(
        repo, "2026_07_27_add_ticket_failure_detail.py", "a7c14f8bd260", "b2e9d4a17c60"
    )
    _commit(
        repo,
        "2026_07_27_reinstate_an_evicted_submission.py",
        "c7a4f1e2b903",
        "a7c14f8bd260",
    )
    return repo


def test_the_stale_branch_was_genuinely_green_against_the_base_it_was_cut_from(
    stale_branch_repo: Path,
) -> None:
    """Why the old check passed: at the time it ran, nothing was wrong.

    This is the run that happened on #505 at 2026-07-27T18:59. It is correct,
    and it stayed on the PR as a green check for the ~21 hours until #505
    merged -- long after it had stopped being true.
    """
    # main as it stood when the branch was cut: b2e9d4a17c60 was the head.
    base = _git(stale_branch_repo, "rev-parse", "main~2").strip()

    result = _run_check(stale_branch_repo, base, "never-disclose")

    assert result.returncode == 0, result.stderr
    assert "merged head f4b7d2c91ae5" in result.stdout


def test_merging_the_stale_branch_into_current_main_is_rejected(
    stale_branch_repo: Path,
) -> None:
    """The guard. Same branch, same content, re-checked against main as it is now.

    Nothing about the branch changed -- only what it would be merging into.
    """
    result = _run_check(stale_branch_repo, "main", "never-disclose")

    assert result.returncode == 1
    assert "would leave 2 Alembic heads" in result.stderr
    assert "c7a4f1e2b903" in result.stderr
    assert "f4b7d2c91ae5" in result.stderr
    assert "2026_07_27_reinstate_an_evicted_submission.py" in result.stderr
    assert "2026_07_27_add_never_disclose_release_policy.py" in result.stderr
    assert "rebase onto current main" in result.stderr
    assert 'alembic merge -m "merge heads" c7a4f1e2b903 f4b7d2c91ae5' in result.stderr


def test_a_migration_missing_only_because_main_moved_is_not_a_deletion(
    stale_branch_repo: Path,
) -> None:
    """Removal is judged against the merge base, not against the base tip.

    The stale branch does not contain #524's migration. It did not delete it
    -- it was cut before it existed -- and reporting that as a deletion would
    bury the real finding.
    """
    result = _run_check(stale_branch_repo, "main", "never-disclose")

    assert "existing migrations were removed" not in result.stderr


def test_rebasing_the_branch_onto_current_main_clears_the_failure(
    stale_branch_repo: Path,
) -> None:
    """The remedy the message recommends actually resolves it."""
    _git(stale_branch_repo, "switch", "-q", "never-disclose")
    _git(stale_branch_repo, "rebase", "-q", "main")
    path = (
        stale_branch_repo
        / "alembic"
        / "versions"
        / "2026_07_27_add_never_disclose_release_policy.py"
    )
    path.write_text(MIGRATION.format(revision="f4b7d2c91ae5", down="c7a4f1e2b903"))
    _git(stale_branch_repo, "commit", "-qam", "repoint onto current main head")

    result = _run_check(stale_branch_repo, "main", "never-disclose")

    assert result.returncode == 0, result.stderr
    assert "merged head f4b7d2c91ae5" in result.stdout


def test_a_branch_that_inherits_a_divergent_base_is_not_blamed_for_it() -> None:
    """`main` broken under a PR is `main`'s fault, and the message says so."""
    base = [
        migration("root", None),
        migration("one", "root"),
        migration("two", "root"),
    ]

    with pytest.raises(MigrationError, match="did not cause the divergence"):
        require_single_merged_head(
            merge_result(base, base), "origin/main", {"one", "two"}
        )
