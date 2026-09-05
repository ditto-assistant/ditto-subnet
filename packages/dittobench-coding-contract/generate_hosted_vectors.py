"""Generate public, unsigned hosted-control projections and signing digests."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from ditto.api_models.coding_hosted import (  # noqa: E402
    HostedCodingRequest,
    HostedCodingResult,
    HostedCodingStatus,
    hosted_message_digest,
)


def vector() -> bytes:
    common = {
        "coding_contract_version": 2,
        "shadow_only": True,
        "weight_eligible": False,
        "evaluation_id": "10000000-0000-4000-8000-000000000001",
        "validator_hotkey": "5" + "A" * 47,
        "artifact_sha256": "1" * 64,
        "assignment_sha256": "2" * 64,
        "policy_sha256": "3" * 64,
        "issued_at_unix": 1788590000,
        "expires_at_unix": 1788590120,
        "signature": "0" * 128,
    }
    request = HostedCodingRequest.model_validate(
        {
            **common,
            "schema": "dittobench-coding-hosted-request-v2",
            "operation": "evaluate",
            "result_sha256": None,
            "nonce": "20000000-0000-4000-8000-000000000002",
        }
    )
    result = HostedCodingResult.model_validate(
        {
            **common,
            "schema": "dittobench-coding-hosted-result-v2",
            "attempt_id": "30000000-0000-4000-8000-000000000003",
            "platform_hotkey": "5" + "B" * 47,
            "request_sha256": hosted_message_digest(request),
            "execution_profile_sha256": "4" * 64,
            "grading_profile_sha256": "5" * 64,
            "evidence_sha256": "6" * 64,
            "outcome": "completed",
        }
    )
    status = HostedCodingStatus.model_validate(
        {
            **{
                key: value
                for key, value in result.model_dump(mode="json", by_alias=True).items()
                if key not in {"schema", "outcome", "evidence_sha256"}
            },
            "schema": "dittobench-coding-hosted-status-v2",
            "state": "admitted",
        }
    )
    data = {
        "schema": "dittobench-coding-hosted-control-vectors-v2",
        "synthetic_only": True,
        "request": request.model_dump(mode="json", by_alias=True),
        "request_signing_sha256": hosted_message_digest(request),
        "result": result.model_dump(mode="json", by_alias=True),
        "result_signing_sha256": hosted_message_digest(result),
        "status": status.model_dump(mode="json", by_alias=True),
        "status_signing_sha256": hosted_message_digest(status),
    }
    return (json.dumps(data, sort_keys=True, separators=(",", ":")) + "\n").encode()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    path = Path(__file__).parent / "testdata/coding_hosted_control_v2.json"
    body = vector()
    if args.check:
        if not path.is_file() or path.read_bytes() != body:
            print("hosted Coding vectors are stale", file=sys.stderr)
            return 1
    else:
        path.write_bytes(body)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
