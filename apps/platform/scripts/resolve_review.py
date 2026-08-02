"""Resolve an ``ath_pending_review`` copy-review hold from the command line.

The anti-copy gate parks a suspected copy in ``ath_pending_review`` (see
``ditto.api_server.scoring_gate``). Under winner-take-all a false-positive hold
costs a legitimate miner everything, so the exit is deliberately manual: an
operator reviews the recorded ``duplicate_of`` / ``review_reason`` and runs this
to either clear the agent back to ``scored`` (re-enters the ledger) or ban it.

Reads Postgres connection from the environment (``POSTGRES_*``; see
``.env.example``). There is intentionally no HTTP surface — this is an owner-only
operation, not part of the public API.

Usage::

    uv run python scripts/resolve_review.py <agent_id> --decision scored
    uv run python scripts/resolve_review.py <agent_id> --decision banned
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from uuid import UUID

from ditto.api_models.agent_status import AgentStatus
from ditto.db import create_db_engine, create_session_maker
from ditto.db.queries.agents import resolve_review

logger = logging.getLogger(__name__)


def _setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )


async def _run(agent_id: UUID, decision: AgentStatus) -> int:
    engine = create_db_engine()
    session_maker = create_session_maker(engine)
    try:
        async with session_maker() as session, session.begin():
            agent = await resolve_review(session, agent_id=agent_id, decision=decision)
        if agent is None:
            logger.error("no agent with id=%s", agent_id)
            return 1
        logger.info("agent %s resolved to %s", agent_id, agent.status)
        return 0
    finally:
        await engine.dispose()


def main() -> int:
    _setup_logging()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("agent_id", type=UUID, help="the held agent's UUID")
    parser.add_argument(
        "--decision",
        required=True,
        choices=["scored", "banned"],
        help="scored = cleared (re-enters ledger); banned = confirmed copy",
    )
    args = parser.parse_args()
    try:
        return asyncio.run(_run(args.agent_id, AgentStatus(args.decision)))
    except ValueError as e:
        # resolve_review raises on a not-held agent / bad decision.
        logger.error("%s", e)
        return 1


if __name__ == "__main__":
    sys.exit(main())
