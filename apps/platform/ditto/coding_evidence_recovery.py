"""Explicit operator-only native ciphertext recovery; no default action."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from uuid import UUID

CONFIRMATION = "RECOVER RESERVED CODING EVIDENCE"


async def run(config_path: Path, *, publish: bool) -> dict:
    from ditto.api_server.coding_evidence_recovery import (
        HostedEvidenceRecovery,
        RecoveryTarget,
    )
    from ditto.api_server.coding_hippius_evidence import (
        parse_hippius_sealed_evidence_config,
    )
    from ditto.api_server.coding_hosted_evidence_spool import HostedEvidenceSpool
    from ditto.api_server.coding_hosted_runtime_config import postgres_config
    from ditto.api_server.coding_hosted_runtime_io import read_json
    from ditto.db.factory import create_db_engine, create_session_maker

    raw = read_json(config_path)
    if (
        not isinstance(raw, dict)
        or raw.get("schema") != "dittobench-coding-evidence-recovery-config-v2"
        or raw.get("shadow_only") is not True
        or raw.get("weight_eligible") is not False
    ):
        raise ValueError("recovery configuration invalid")
    item = raw["target"]
    target = RecoveryTarget(
        phase=item["phase"],
        worker_id=UUID(item["worker_id"]),
        evaluation_id=UUID(item["evaluation_id"]),
        attempt_id=UUID(item["attempt_id"]),
        identity_sha256=item["identity_sha256"],
        request_id=UUID(item["request_id"])
        if item.get("request_id") is not None
        else None,
    )
    target.check()
    database, _ = postgres_config(
        read_json(Path(raw["postgres_environment_file"]), 128 << 10)
    )
    config, probe = None, None
    if publish:
        environment = read_json(Path(raw["hippius_environment_file"]))
        allowed = {
            "DITTO_CODING_HIPPIUS_" + suffix
            for suffix in (
                "ENDPOINT_URL",
                "SEALED_EVIDENCE_BUCKET",
                "EVIDENCE_MEDIATOR_ACCESS_KEY",
                "EVIDENCE_MEDIATOR_SECRET_KEY",
                "REGION",
                "TIMEOUT_SECONDS",
            )
        }
        if (
            not isinstance(environment, dict)
            or set(environment) - allowed
            or any(type(v) is not str for v in environment.values())
        ):
            raise ValueError("recovery storage configuration invalid")
        config = parse_hippius_sealed_evidence_config(environment)
        probe = Path(raw["probe_receipt_file"])
    spool = HostedEvidenceSpool(
        Path(raw["spool_root"]),
        max_bytes=raw.get("max_bytes", 2 << 30),
        max_objects=raw.get("max_objects", 4096),
        read_only=True,
    )
    engine = None
    try:
        engine = create_db_engine(database)
        recovery = HostedEvidenceRecovery(
            sessions=create_session_maker(engine),
            spool=spool,
            config=config,
            probe_receipt=probe,
        )
        return await (recovery.resume(target) if publish else recovery.inspect(target))
    finally:
        try:
            if engine is not None:
                await engine.dispose()
        finally:
            spool.close()


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Exact reserved ciphertext recovery; no execution replay"
    )
    modes = parser.add_mutually_exclusive_group(required=True)
    modes.add_argument("--inspect", action="store_true")
    modes.add_argument("--resume-reserved", action="store_true")
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--confirm")
    args = parser.parse_args()
    if (args.resume_reserved and args.confirm != CONFIRMATION) or (
        args.inspect and args.confirm is not None
    ):
        parser.error(
            "resume requires the exact recovery confirmation; inspect takes none"
        )
    try:
        result = asyncio.run(run(args.config, publish=args.resume_reserved))
        print(json.dumps(result, sort_keys=True, separators=(",", ":")))
        return 0
    except Exception:
        print("native evidence recovery unavailable", file=sys.stderr)
        return 70


if __name__ == "__main__":
    raise SystemExit(main())
