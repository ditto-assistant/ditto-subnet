"""One bounded stdin/stdout start transaction. Never mounted by the public API."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from uuid import UUID

from ditto.api_models.coding_canonical import coding_canonical_json_bytes
from ditto.api_models.coding_hosted_start import HostedStartRequest
from ditto.api_server.coding_hosted_start import HostedStartStore
from ditto.db.factory import create_db_engine, create_session_maker


def _unique(items: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in items:
        if key in result:
            raise ValueError("duplicate start field")
        result[key] = value
    return result


async def transact(body: bytes, worker_id: UUID) -> bytes:
    if not 0 < len(body) <= 16384:
        raise ValueError("invalid start size")
    parsed = json.loads(body, object_pairs_hook=_unique)
    request = HostedStartRequest.model_validate(parsed)
    engine = create_db_engine()
    try:
        fresh = await HostedStartStore(
            create_session_maker(engine), worker_id
        ).commit_start(request)
    finally:
        await engine.dispose()
    return coding_canonical_json_bytes(
        {
            "schema": "dittobench-coding-hosted-start-result-v2",
            "request_sha256": request.digest(),
            "newly_started": fresh,
        },
        maximum_bytes=1024,
        label="hosted start result",
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--worker-id", required=True, type=UUID)
    args = parser.parse_args()
    # Never place database exceptions, URLs or credentials on the worker pipe.
    logging.disable(logging.CRITICAL)
    try:
        body = sys.stdin.buffer.read(16385)
        result = asyncio.run(transact(body, args.worker_id))
        sys.stdout.buffer.write(result)
        sys.stdout.buffer.flush()
    except Exception:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
