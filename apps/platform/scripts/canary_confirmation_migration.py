#!/usr/bin/env python
"""Rehearse the LongMem confirmation migration against a disposable database.

There is no standing prod-copy environment for Platform, so this is the canary:
point it at a database and it restores (optionally from a real ``pg_dump``),
runs the full Alembic chain, then proves the things the migration is supposed to
change -- including one insert that is *impossible* before it and one that must
still be refused after it.

Usage
-----
Rehearse on a schema built from the migration chain::

    uv run python scripts/canary_confirmation_migration.py

Rehearse against a real production copy (the one that matters)::

    # Reads prod; writes nothing to it. Postgres runs in compose on the VM as
    # user/database ``ditto`` (apps/platform/docker-compose.yml).
    gcloud compute ssh ditto-platform-prod --zone us-central1-a \\
      --tunnel-through-iap --command \\
      "docker exec \\$(docker ps -qf name=postgres) pg_dump -U ditto -d ditto \\
       --format=custom --no-owner --no-privileges" > /tmp/prod.dump

    uv run python scripts/canary_confirmation_migration.py --dump /tmp/prod.dump

The dump is READ-ONLY with respect to production. Nothing here connects to a
production database: the restore target is always a scratch database this script
creates and drops.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import re
import subprocess
import sys
import uuid
from dataclasses import dataclass
from pathlib import Path

import asyncpg

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ditto.tests import pgharness  # noqa: E402

_MIGRATION = "3f5c81a7d940"
_RELAXED_CONSTRAINTS = (
    ("confirmation_retest_authorizations", "confirmation_retest_version_check"),
    ("confirmation_bundles", "confirmation_bundles_version_check"),
    ("confirmation_bundle_subjects", "confirmation_subjects_version_check"),
)
_FLOOR_PIN = re.compile(r"bench_version\s*>=\s*9")
_EXACT_PIN = re.compile(r"bench_version\s*=\s*9")

_SUBJECT_INSERT = """
    INSERT INTO confirmation_bundle_subjects (
        agent_id, bench_version, artifact_sha256, result_status,
        base_evidence_sha256, base_quality_micros, base_stderr_micros,
        base_model_factor_bps, base_tool_factor_bps
    ) VALUES ($1, $2, $3, 'base_only', $4, 900000, 1000, 10000, 10000)
"""


@dataclass
class Check:
    name: str
    ok: bool
    detail: str

    def render(self) -> str:
        return f"{'PASS' if self.ok else 'FAIL'}  {self.name}\n        {self.detail}"


async def _checks_mentioning(
    conn: asyncpg.Connection, table: str, column: str
) -> list[str]:
    """Every CHECK on ``table`` whose definition references ``column``.

    Deliberately not name-based: Alembic's naming convention rewrites the short
    name we pass into ``ck_<table>_<fragment>_<hash>`` and truncates the
    fragment, so matching names reports a correct constraint as missing. The
    definition is what actually governs the data.
    """
    rows = await conn.fetch(
        """
        SELECT pg_get_constraintdef(c.oid) AS definition
        FROM pg_constraint c
        JOIN pg_class t ON t.oid = c.conrelid
        WHERE t.relname = $1 AND c.contype = 'c'
        """,
        table,
    )
    return [row["definition"] for row in rows if column in row["definition"]]


async def _try_subject(conn: asyncpg.Connection, bench_version: int) -> str | None:
    """Insert and roll back one subject; return the refusal message, if any.

    Everything happens inside a transaction that is always rolled back, so this
    probes the live constraints without leaving a row behind -- which is what
    makes it safe to point at a restored production copy.
    """
    agent_id = uuid.uuid4()
    digest = uuid.uuid4().hex * 2
    transaction = conn.transaction()
    await transaction.start()
    try:
        await conn.execute(
            """
            INSERT INTO agents (agent_id, miner_hotkey, name, sha256, status)
            VALUES ($1, $2, 'canary-probe', $3, 'scored')
            """,
            agent_id,
            f"5Canary{agent_id.hex[:12]}",
            digest,
        )
        await conn.execute(_SUBJECT_INSERT, agent_id, bench_version, digest, "b" * 64)
    except asyncpg.PostgresError as error:
        await transaction.rollback()
        return str(error).strip()
    await transaction.rollback()
    return None


async def canary(dsn: str, *, restored_from: str | None) -> list[Check]:
    checks: list[Check] = []
    conn = await asyncpg.connect(dsn)
    try:
        counts = {}
        for table, _ in _RELAXED_CONSTRAINTS:
            counts[table] = await conn.fetchval(f"SELECT count(*) FROM {table}")  # noqa: S608
        checks.append(
            Check(
                "database under test",
                True,
                f"source={restored_from or 'migration chain'} rows={counts}",
            )
        )

        for table, _name in _RELAXED_CONSTRAINTS:
            found = await _checks_mentioning(conn, table, "bench_version")
            relaxed = any(_FLOOR_PIN.search(expression) for expression in found)
            # `"= 9)" in ">= 9)"` is True, so the exact-equality probe has to be
            # anchored -- a sloppy substring here reported the relaxed
            # constraint as still pinned.
            exact = any(_EXACT_PIN.search(expression) for expression in found)
            checks.append(
                Check(
                    f"{table} bench-version pin relaxed",
                    relaxed and not exact,
                    f"definitions={found or 'none'}",
                )
            )

        columns = {
            row["column_name"]
            for row in await conn.fetch(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name = 'confirmation_bundle_tickets'"
            )
        }
        checks.append(
            Check(
                "ticket diagnostics present",
                {"failure_class", "failure_stage"} <= columns,
                "failure_class/failure_stage exist on confirmation_bundle_tickets",
            )
        )

        # The point of the migration, observed rather than argued: before it,
        # the CHECK constraint rejects this row outright.
        refusal = await _try_subject(conn, 11)
        checks.append(
            Check(
                "bench-11 subject accepted",
                refusal is None,
                refusal or "inserted and rolled back (rejected before this migration)",
            )
        )

        # And the floor still holds for epochs with no base evidence at all.
        refusal = await _try_subject(conn, 8)
        checks.append(
            Check(
                "bench-8 subject still refused",
                refusal is not None,
                refusal or "FLOOR BREACHED: a pre-contract row was accepted",
            )
        )

        diagnostics = await _checks_mentioning(
            conn, "confirmation_bundle_tickets", "failure_class"
        )
        pair_guarded = any(
            "failure_stage" in expression and "IS NULL" in expression
            for expression in diagnostics
        )
        allowlisted = any("sandbox_oom" in expression for expression in diagnostics)
        checks.append(
            Check(
                "diagnostics are paired and allowlisted",
                pair_guarded and allowlisted,
                f"definitions={diagnostics or 'none'}",
            )
        )
    finally:
        await conn.close()
    return checks


def _alembic(admin: pgharness.Dsn, database: str) -> subprocess.CompletedProcess:
    """Run the chain the way Platform does: POSTGRES_* env, not a DSN string."""
    env = dict(os.environ)
    env |= {
        "POSTGRES_USER": admin.user,
        "POSTGRES_PASSWORD": admin.password,
        "POSTGRES_HOST": admin.host,
        "POSTGRES_PORT": str(admin.port),
        "POSTGRES_DB": database,
    }
    return subprocess.run(
        ["uv", "run", "alembic", "upgrade", "head"],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )


async def run(args: argparse.Namespace, admin: pgharness.Dsn) -> int:
    base = f"postgresql://{admin.user}:{admin.password}@{admin.host}:{admin.port}"
    scratch = f"canary_{uuid.uuid4().hex[:10]}"
    admin_dsn = f"{base}/postgres"
    scratch_dsn = f"{base}/{scratch}"

    conn = await asyncpg.connect(admin_dsn)
    try:
        await conn.execute(f'CREATE DATABASE "{scratch}"')
    finally:
        await conn.close()
    print(f"scratch database: {scratch}")

    try:
        if args.dump:
            restore = subprocess.run(
                [
                    "pg_restore",
                    "--no-owner",
                    "--no-privileges",
                    "--dbname",
                    scratch_dsn,
                    str(args.dump),
                ],
                capture_output=True,
                text=True,
                check=False,
            )
            if restore.returncode != 0:
                # pg_restore warns on roles/extensions it cannot recreate; those
                # are not fatal for a schema+data rehearsal.
                print(restore.stderr[-2000:], file=sys.stderr)
                print("pg_restore reported errors (continuing; see above)")

        migrate = _alembic(admin, scratch)
        if migrate.returncode != 0:
            print(migrate.stdout[-4000:])
            print(migrate.stderr[-4000:], file=sys.stderr)
            print("\nFAIL  alembic upgrade head")
            return 1
        log = migrate.stdout + migrate.stderr
        print(
            f"alembic upgrade head: ok "
            f"({'applied ' + _MIGRATION if _MIGRATION in log else 'already at head'})"
        )

        checks = await canary(
            scratch_dsn, restored_from=str(args.dump) if args.dump else None
        )
        print()
        for check in checks:
            print(check.render())
        failed = [check for check in checks if not check.ok]
        print(f"\n{len(checks) - len(failed)}/{len(checks)} checks passed")
        return 1 if failed else 0
    finally:
        if args.keep:
            print(f"kept scratch database: {scratch_dsn}")
        else:
            conn = await asyncpg.connect(admin_dsn)
            try:
                await conn.execute(f'DROP DATABASE IF EXISTS "{scratch}" WITH (FORCE)')
            finally:
                await conn.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dump",
        type=Path,
        default=None,
        help="pg_dump custom-format file to restore before migrating (a prod copy)",
    )
    parser.add_argument(
        "--keep", action="store_true", help="keep the scratch database for inspection"
    )
    # pgharness provisions the ambient container with its own asyncio.run, so
    # it has to resolve before this process owns an event loop.
    admin = pgharness.resolve_admin_dsn()
    return asyncio.run(run(parser.parse_args(), admin))


if __name__ == "__main__":
    raise SystemExit(main())
