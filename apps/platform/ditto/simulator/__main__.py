"""CLI entry point: ``uv run python -m ditto.simulator <scenario> [options]``.

Loads DB config from the same ``POSTGRES_*`` env vars the API server uses
(source ``.env`` first), wipes the simulator tables (unless ``--no-wipe``),
applies the named scenario, and prints a row-count summary.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import UTC, datetime

from sqlalchemy import text

from ditto.db.factory import create_db_engine, create_session_maker
from ditto.simulator.fabric import SIMULATOR_TABLES, Fabric, FabricConfig, wipe_all
from ditto.simulator.scenarios import Scenario, ScenarioContext, discover_scenarios

DEFAULT_SEED = 118

# Tables worth surfacing in the post-run summary (subset of SIMULATOR_TABLES).
_SUMMARY_TABLES = (
    "agents",
    "scores",
    "validator_tickets",
    "screening_attempts",
    "screening_quarantines",
    "ath_reviews",
    "validator_heartbeats",
    "screener_heartbeats",
    "score_audit_log",
    "evaluation_payments",
)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m ditto.simulator",
        description=(
            "Seed the local Postgres with a named, deterministic scenario so "
            "the public dashboard can be exercised without chain/mainnet. "
            "Local-dev only."
        ),
    )
    parser.add_argument("scenario", nargs="?", help="scenario name (see --list)")
    parser.add_argument(
        "--seed",
        type=int,
        default=DEFAULT_SEED,
        help=f"deterministic fabrication seed (default {DEFAULT_SEED})",
    )
    parser.add_argument(
        "--no-wipe",
        action="store_true",
        help="skip the pre-scenario TRUNCATE of simulator tables",
    )
    parser.add_argument(
        "--list", action="store_true", help="list available scenarios and exit"
    )
    return parser


async def _run(scenario: Scenario, *, seed: int, wipe: bool) -> None:
    engine = create_db_engine()
    try:
        session_maker = create_session_maker(engine)
        if wipe:
            async with session_maker() as session, session.begin():
                await wipe_all(session)
            print(f"wiped {len(SIMULATOR_TABLES)} simulator tables")
        fabric = Fabric(FabricConfig(seed=seed, now=datetime.now(UTC)))
        ctx = ScenarioContext(session_maker=session_maker, fabric=fabric)
        await scenario.apply(ctx)
        print(f"applied scenario {scenario.name!r} (seed={seed})")
        async with session_maker() as session:
            for table in _SUMMARY_TABLES:
                count = await session.scalar(
                    text(f"SELECT count(*) FROM {table}")  # noqa: S608 - fixed names
                )
                if count:
                    print(f"  {table}: {count}")
    finally:
        await engine.dispose()
    print(
        "note: /api/v1/public/* responses are cached up to 30s "
        "(restart the API with PUBLIC_CACHE_DISABLED=1 for instant reads)"
    )


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    scenarios = discover_scenarios()
    if args.list:
        width = max(len(name) for name in scenarios)
        for scenario in scenarios.values():
            print(f"{scenario.name:<{width}}  {scenario.description}")
        return 0
    if args.scenario is None:
        parser.error("a scenario name is required (or use --list)")
    if args.scenario not in scenarios:
        available = ", ".join(scenarios)
        parser.error(f"unknown scenario {args.scenario!r}; available: {available}")
    asyncio.run(_run(scenarios[args.scenario], seed=args.seed, wipe=not args.no_wipe))
    return 0


if __name__ == "__main__":
    sys.exit(main())
