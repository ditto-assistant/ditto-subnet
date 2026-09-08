"""Explicit read-only canary evidence inspection; never launch or repair a run."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from contextlib import ExitStack
from pathlib import Path
from uuid import UUID


async def run(path: Path) -> dict:
    import bittensor

    from ditto.api_models.coding_hosted import hosted_message_digest
    from ditto.api_server.coding_canary_acceptance import (
        CanaryEvidenceTarget,
        CanaryEvidenceVerifier,
        Phase,
    )
    from ditto.api_server.coding_evidence_recovery import HostedEvidenceRecovery
    from ditto.api_server.coding_hippius_evidence import (
        parse_hippius_sealed_evidence_config,
    )
    from ditto.api_server.coding_hosted_evidence_spool import HostedEvidenceSpool
    from ditto.api_server.coding_hosted_runtime_config import postgres_config
    from ditto.api_server.coding_hosted_runtime_io import read_json, read_private
    from ditto.api_server.coding_hosted_verification import (
        HostedResultExpectation,
        verify_hosted_result,
    )
    from ditto.db.factory import create_db_engine, create_session_maker

    raw = read_json(path)
    if (
        not isinstance(raw, dict)
        or raw.get("schema") != "dittobench-coding-canary-evidence-config-v2"
        or raw.get("shadow_only") is not True
        or raw.get("weight_eligible") is not False
    ):
        raise ValueError("canary evidence configuration invalid")
    values = raw["expected"]
    expected = HostedResultExpectation(
        evaluation_id=UUID(values["evaluation_id"]),
        attempt_id=UUID(values["attempt_id"]),
        validator_hotkey=values["validator_hotkey"],
        platform_hotkey=values["platform_hotkey"],
        artifact_sha256=values["artifact_sha256"],
        assignment_sha256=values["assignment_sha256"],
        policy_sha256=values["policy_sha256"],
        execution_profile_sha256=values["execution_profile_sha256"],
        grading_profile_sha256=values["grading_profile_sha256"],
        request_sha256=values["request_sha256"],
    )
    target = CanaryEvidenceTarget(
        expected, UUID(raw["worker_id"]), raw["result_sha256"], raw["terminal_sha256"]
    )
    target.check()
    body = read_private(Path(raw["signed_result_file"]), 8192)
    verifiers = {
        expected.platform_hotkey: bittensor.Keypair(
            ss58_address=expected.platform_hotkey
        )
    }
    # Bad envelopes do not cause database or storage credential files to be read.
    verified = verify_hosted_result(
        body=body,
        expected=expected,
        trusted_verifiers=verifiers,
        now_unix=int(time.time()),
    )
    if (
        hosted_message_digest(verified) != target.result_sha256
        or verified.evidence_sha256 != target.terminal_sha256
        or verified.outcome != "completed"
    ):
        raise ValueError("canary signed outcome differs")
    database, _ = postgres_config(
        read_json(Path(raw["postgres_environment_file"]), 128 << 10)
    )
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
        or any(type(value) is not str for value in environment.values())
    ):
        raise ValueError("canary storage configuration invalid")
    storage = parse_hippius_sealed_evidence_config(environment)
    roots = raw["spool_roots"]
    if not isinstance(roots, dict) or set(roots) != {
        "inference",
        "authoring",
        "terminal",
    }:
        raise ValueError("canary spool phases incomplete")
    engine = None
    with ExitStack() as stack:
        spools = {}
        for root in set(roots.values()):
            spool = HostedEvidenceSpool(
                Path(root), max_bytes=2 << 30, max_objects=4096, read_only=True
            )
            stack.callback(spool.close)
            spools[root] = spool
        try:
            engine = create_db_engine(database)
            sessions = create_session_maker(engine)
            phases: tuple[Phase, ...] = ("inference", "authoring", "terminal")
            readbacks = {
                phase: HostedEvidenceRecovery(
                    sessions=sessions,
                    spool=spools[roots[phase]],
                    config=storage,
                    probe_receipt=Path(raw["probe_receipt_file"]),
                )
                for phase in phases
            }
            verifier = CanaryEvidenceVerifier(
                sessions=sessions, readbacks=readbacks, trusted_verifiers=verifiers
            )
            return await verifier.verify(body, target)
        finally:
            if engine is not None:
                await engine.dispose()


def main() -> int:
    from ditto.api_server.coding_canary_acceptance import (
        CanaryEvidenceVerificationError,
    )

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--verify-evidence", action="store_true", required=True)
    parser.add_argument("--config", type=Path, required=True)
    args = parser.parse_args()
    try:
        result = asyncio.run(run(args.config))
        print(json.dumps(result, sort_keys=True, separators=(",", ":")))
        return 0
    except CanaryEvidenceVerificationError as error:
        print(
            f"native canary evidence verification unavailable: {error.stage}",
            file=sys.stderr,
        )
        return 70
    except (Exception, KeyboardInterrupt):
        print("native canary evidence verification unavailable", file=sys.stderr)
        return 70


if __name__ == "__main__":
    raise SystemExit(main())
