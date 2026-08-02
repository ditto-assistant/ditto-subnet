"""Ban or unban a miner hotkey from the command line (owner-only).

A hotkey-level ban blocks the *miner* — every future ``/upload/agent`` — and is
surfaced on ``/retrieval/agent-by-hotkey``. This is distinct from resolving a
single ``ath_pending_review`` hold (``scripts/resolve_review.py``), which bans one
agent. Typical use: an operator confirms a copy via the review flow, bans that
agent, then bans the hotkey here so the miner can't just re-upload.

Reads Postgres connection from the environment (``POSTGRES_*``; see
``.env.example``). There is intentionally no HTTP surface — this is an owner-only
operation, not part of the public API.

Usage::

    uv run python scripts/ban_hotkey.py <hotkey> --reason "confirmed copy of <agent_id>"
    uv run python scripts/ban_hotkey.py <hotkey> --unban
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys

from ditto.db import create_db_engine, create_session_maker
from ditto.db.queries.bans import ban_hotkey, unban_hotkey

logger = logging.getLogger(__name__)


def _setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )


async def _run(hotkey: str, *, unban: bool, reason: str | None) -> int:
    engine = create_db_engine()
    session_maker = create_session_maker(engine)
    try:
        async with session_maker() as session, session.begin():
            if unban:
                removed = await unban_hotkey(session, hotkey=hotkey)
            else:
                added = await ban_hotkey(session, hotkey=hotkey, reason=reason)
        if unban:
            if not removed:
                logger.info("hotkey %s was not banned; nothing to do", hotkey)
                return 0
            logger.info("hotkey %s unbanned", hotkey)
            return 0
        if not added:
            logger.info("hotkey %s was already banned; left unchanged", hotkey)
            return 0
        logger.info("hotkey %s banned", hotkey)
        return 0
    finally:
        await engine.dispose()


def main() -> int:
    _setup_logging()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("hotkey", help="the miner's SS58 hotkey")
    parser.add_argument(
        "--unban", action="store_true", help="remove an existing ban instead of adding"
    )
    parser.add_argument(
        "--reason", default=None, help="audit note recorded with the ban"
    )
    args = parser.parse_args()
    return asyncio.run(_run(args.hotkey, unban=args.unban, reason=args.reason))


if __name__ == "__main__":
    sys.exit(main())
