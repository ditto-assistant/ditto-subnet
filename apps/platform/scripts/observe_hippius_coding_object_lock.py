"""Run one disposable, confirmation-gated Hippius Object Lock canary."""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

from ditto.api_server.coding_hippius_object_lock import (
    HIPPIUS_OBJECT_LOCK_CONFIRMATION,
    AiobotoHippiusObjectLockTransport,
    HippiusObjectLockCanaryConfig,
    run_hippius_object_lock_canary,
    write_hippius_object_lock_receipt,
)
from ditto.api_server.coding_hippius_probe import (
    HippiusProbeCredential,
    HippiusProbeError,
    resolve_repository_source_sha,
)


def _required_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise ValueError(f"{name} must be configured")
    return value


async def _run(args: argparse.Namespace) -> str:
    config = HippiusObjectLockCanaryConfig(
        endpoint_url=_required_env("DITTO_CODING_HIPPIUS_ENDPOINT_URL"),
        master_credential=HippiusProbeCredential(
            access_key=_required_env("DITTO_CODING_HIPPIUS_OBJECT_LOCK_ACCESS_KEY"),
            secret_key=_required_env("DITTO_CODING_HIPPIUS_OBJECT_LOCK_SECRET_KEY"),
        ),
    )
    receipt = await run_hippius_object_lock_canary(
        config=config,
        transport=AiobotoHippiusObjectLockTransport(config),
        source_sha=resolve_repository_source_sha(Path(__file__).parents[2]),
        provider_profile_payload_sha256=args.provider_profile_payload_sha256,
    )
    return write_hippius_object_lock_receipt(receipt=receipt, output=args.output)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--provider-profile-payload-sha256", required=True)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--confirm", required=True)
    args = parser.parse_args(argv)
    if args.confirm != HIPPIUS_OBJECT_LOCK_CONFIRMATION:
        parser.error("--confirm must be exactly: " + HIPPIUS_OBJECT_LOCK_CONFIRMATION)
    try:
        digest = asyncio.run(_run(args))
    except (HippiusProbeError, ValueError):
        print("Hippius Object Lock canary failed", file=sys.stderr)
        return 2
    except Exception:
        print(
            "Hippius Object Lock canary failed: unexpected internal error",
            file=sys.stderr,
        )
        return 2
    print(f"Hippius Object Lock canary ready; receipt_payload_sha256={digest}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
