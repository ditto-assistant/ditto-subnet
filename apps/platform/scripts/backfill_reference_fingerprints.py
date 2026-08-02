"""Recompute stored anti-copy fingerprints with the reference-aware algorithm.

The command is read-only unless ``--apply`` is passed. It changes fingerprint
metadata only; agent status, scores, duplicate attribution, and artifacts are
never mutated.

Rollout contract: deploy the matching platform version first, then deploy the
durable ATH migration/API and snapshot legacy hold evidence before running even the
dry pass. Review dry-run aggregate counts before separately authorizing
``--apply``, then run a catch-up pass. During the bounded transition, score-close
cross-version/corpus comparisons enter individual operator review as explicitly
inconclusive; they never fall through to structural or size evidence.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from typing import Protocol

from sqlalchemy import select
from sqlalchemy.orm import undefer_group

from ditto.api_server.endpoints.upload import DEFAULT_MAX_TARBALL_SIZE_BYTES
from ditto.api_server.fingerprint import (
    _FP_VERSION,
    compute_content_fingerprint,
    compute_normalized_source_hash,
    compute_prompt_fingerprint,
    reference_corpus_provenance,
)
from ditto.api_server.storage import (
    ObjectDownloadFailedError,
    create_storage_client,
    parse_storage_config_from_env,
)
from ditto.db import create_db_engine, create_session_maker
from ditto.db.models import Agent

logger = logging.getLogger(__name__)
_DEFAULT_BATCH_SIZE = 100


class _FingerprintMetadata(Protocol):
    content_fingerprint: dict | None
    normalized_source_hash: str | None
    prompt_fingerprint: dict | None


def _is_current(agent: _FingerprintMetadata) -> bool:
    fingerprint = agent.content_fingerprint
    return bool(
        fingerprint
        and fingerprint.get("v") == _FP_VERSION
        and fingerprint.get("corpus") == reference_corpus_provenance()["corpus_id"]
    )


def _store_fingerprint_metadata(
    agent: _FingerprintMetadata,
    *,
    content: dict,
    normalized: str | None,
    prompt: dict | None,
) -> None:
    """Update only the three anti-copy metadata fields on an ORM object."""
    agent.content_fingerprint = content
    agent.normalized_source_hash = normalized
    agent.prompt_fingerprint = prompt


async def _run(*, apply: bool, limit: int | None, batch_size: int) -> int:
    engine = create_db_engine()
    session_maker = create_session_maker(engine)
    storage_config = parse_storage_config_from_env()
    inspected = stale = updated = failed = 0
    try:
        async with (
            create_storage_client(storage_config) as storage,
            session_maker() as session,
        ):
            last_agent_id = None
            stop = False
            while not stop:
                statement = (
                    select(Agent)
                    # This script reads + rewrites the deferred sketch columns.
                    .options(undefer_group("anticopy"))
                    .order_by(Agent.agent_id)
                    .limit(batch_size)
                )
                if last_agent_id is not None:
                    statement = statement.where(Agent.agent_id > last_agent_id)
                agents = (await session.scalars(statement)).all()
                if not agents:
                    break
                for agent in agents:
                    if limit is not None and updated >= limit:
                        stop = True
                        break
                    inspected += 1
                    if _is_current(agent):
                        continue
                    stale += 1
                    try:
                        tar_bytes = await storage.get_object(
                            key=f"{agent.agent_id}/agent.tar.gz",
                            max_bytes=DEFAULT_MAX_TARBALL_SIZE_BYTES,
                        )
                    except ObjectDownloadFailedError:
                        failed += 1
                        continue
                    content, normalized, prompt = await asyncio.gather(
                        asyncio.to_thread(compute_content_fingerprint, tar_bytes),
                        asyncio.to_thread(compute_normalized_source_hash, tar_bytes),
                        asyncio.to_thread(compute_prompt_fingerprint, tar_bytes),
                    )
                    if content is None:
                        failed += 1
                        continue
                    if apply:
                        _store_fingerprint_metadata(
                            agent,
                            content=content,
                            normalized=normalized,
                            prompt=prompt,
                        )
                    updated += 1
                if apply:
                    await session.commit()
                last_agent_id = agents[-1].agent_id
    finally:
        await engine.dispose()
    logger.info(
        "fingerprint backfill complete apply=%s inspected=%d stale=%d "
        "processed=%d failed=%d",
        apply,
        inspected,
        stale,
        updated,
        failed,
    )
    return 1 if failed else 0


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--apply", action="store_true", help="persist recomputed fingerprint metadata"
    )
    parser.add_argument("--limit", type=int, help="maximum stale artifacts to process")
    parser.add_argument(
        "--batch-size",
        type=int,
        default=_DEFAULT_BATCH_SIZE,
        help=f"rows fetched per bounded batch (default: {_DEFAULT_BATCH_SIZE})",
    )
    args = parser.parse_args()
    if args.limit is not None and args.limit <= 0:
        parser.error("--limit must be positive")
    if args.batch_size <= 0:
        parser.error("--batch-size must be positive")
    return asyncio.run(
        _run(apply=args.apply, limit=args.limit, batch_size=args.batch_size)
    )


if __name__ == "__main__":
    sys.exit(main())
