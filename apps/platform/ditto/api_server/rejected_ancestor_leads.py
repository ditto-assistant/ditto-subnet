"""Create source-free reviewer hints from verified rejected source artifacts."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
from typing import TYPE_CHECKING
from uuid import UUID

from ditto.api_server.source_inspect import (
    MAX_TARBALL_BYTES,
    SourceInspectError,
    TarSourceInspector,
)
from ditto.api_server.storage import ObjectDownloadFailedError, ObjectNotFoundError
from ditto.db.queries.rejected_ancestors import RejectedAncestor, rejected_ancestors
from ditto_screening_protocol.rejected_ancestor import (
    MAX_ANCESTOR_WINDOWS,
    MAX_WINDOW_TOKENS,
    RejectedAncestorWindow,
    rolling_hash,
    source_tokens,
    window_sha256,
)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from ditto.api_server.storage import S3StorageClient
    from ditto.db.models import Agent

_CITATION = re.compile(r"([A-Za-z0-9_./+-]+):L?(\d+)(?:-L?(\d+))?")
# Supplemental history must fit within the worker's 60-second API timeout.
# At most three old objects are considered; none may hold a valid fetch open.
_ANCESTOR_SOURCE_TIMEOUT_SECONDS = 10.0
logger = logging.getLogger(__name__)


def artifact_windows(
    ancestor: RejectedAncestor, data: bytes
) -> list[RejectedAncestorWindow]:
    if hashlib.sha256(data).hexdigest() != ancestor.sha256:
        raise ValueError("rejected ancestor artifact digest mismatch")
    inspector = TarSourceInspector(data)
    result: list[RejectedAncestorWindow] = []
    texts: dict[str, list[str]] = {}
    seen: set[str] = set()
    for reference in ancestor.references:
        match = _CITATION.fullmatch(reference.strip().strip("`"))
        if match is None:
            continue
        path, line, last = match.groups()
        start, end = int(line), int(last or line)
        if start < 1 or end < start:
            continue
        try:
            canonical_path = RejectedAncestorWindow.safe_path(path.removeprefix("./"))
            if len(canonical_path) > 240:
                continue
            if path not in texts:
                texts[path] = inspector.read_full_text(path).splitlines()
            lines = texts[path]
        except (SourceInspectError, ValueError):
            continue
        if end > len(lines):
            continue
        # Context around a point citation; bounded windows over an explicit
        # span. A long cited function must not disappear solely due to length.
        if last is None:
            start, end = max(1, start - 2), min(len(lines), end + 2)
        for first in range(start, end + 1, 5):
            final = min(first + 4, end)
            tokens = [
                token for token, _ in source_tokens("\n".join(lines[first - 1 : final]))
            ]
            if not 8 <= len(tokens) <= MAX_WINDOW_TOKENS:
                continue
            digest = window_sha256(tokens)
            if digest in seen:
                continue
            seen.add(digest)
            result.append(
                RejectedAncestorWindow(
                    agent_id=ancestor.agent_id,
                    artifact_sha256=ancestor.sha256,
                    path=canonical_path,
                    start_line=first,
                    end_line=final,
                    token_count=len(tokens),
                    sha256=digest,
                    rolling_hash=rolling_hash(tokens),
                )
            )
            if len(result) >= MAX_ANCESTOR_WINDOWS:
                return result

    return result


async def rejected_ancestor_windows(
    session: AsyncSession, storage: S3StorageClient, *, candidate: Agent
) -> tuple[list[RejectedAncestorWindow], list[UUID]]:
    result: list[RejectedAncestorWindow] = []
    unavailable: list[UUID] = []
    for ancestor in await rejected_ancestors(session, candidate=candidate):
        try:
            async with asyncio.timeout(_ANCESTOR_SOURCE_TIMEOUT_SECONDS):
                data = await storage.get_object(
                    key=f"{ancestor.agent_id}/agent.tar.gz", max_bytes=MAX_TARBALL_BYTES
                )
                result.extend(await asyncio.to_thread(artifact_windows, ancestor, data))
        except (
            ObjectDownloadFailedError,
            ObjectNotFoundError,
            SourceInspectError,
            ValueError,
            TimeoutError,
        ) as error:
            # Historical attention is supplemental. A purged/unavailable old
            # object must not deny the current, valid lease its source fetch.
            # Surface the gap to the reviewer/operator; never invent a match.
            unavailable.append(ancestor.agent_id)
            logger.warning(
                "rejected ancestor source unavailable agent_id=%s error_type=%s",
                ancestor.agent_id,
                type(error).__name__,
            )
        if len(result) >= MAX_ANCESTOR_WINDOWS:
            break
    return result[:MAX_ANCESTOR_WINDOWS], unavailable
