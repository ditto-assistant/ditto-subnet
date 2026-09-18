"""Run a Platform-issued conversation claim against a provisioned sandbox.

The launcher must verify the screened image archive and provide a fresh sandbox
with a trusted relay capped at the claim's harness budget. This command neither
builds untrusted source nor provisions an unrestricted inference endpoint.
"""

from __future__ import annotations

import argparse
import asyncio
import os
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from ditto_screener.conversation import AstraExaminer, Limits, MemoryHarness, evaluate
from ditto_screening_protocol.conversation import ConversationClaim


async def run(
    claim_path: Path, harness_url: str, output: Path, platform_url: str | None = None
) -> int:
    claim = ConversationClaim.model_validate_json(claim_path.read_text())
    remaining = (claim.expires_at - datetime.now(UTC)).total_seconds()
    if remaining <= 0:
        raise ValueError("conversation claim has expired")
    key = os.environ.get("OPENAI_API_KEY")
    if not key:
        raise ValueError("OPENAI_API_KEY is required in the trusted examiner process")
    platform_token = os.environ.get("DITTO_PLATFORM_ADMIN_TOKEN")
    if platform_url:
        parsed = urlsplit(platform_url)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("Platform submission requires an HTTPS origin")
        if not platform_token:
            raise ValueError(
                "DITTO_PLATFORM_ADMIN_TOKEN is required to submit the report"
            )
    if output.exists():
        raise ValueError(
            "report already exists; resubmit it without rerunning the judge"
        )
    limits = Limits(total_seconds=min(3600, remaining))
    async with (
        httpx.AsyncClient(
            timeout=120, follow_redirects=False, trust_env=False
        ) as harness_client,
        httpx.AsyncClient(
            timeout=120,
            follow_redirects=False,
            trust_env=False,
            headers={"Authorization": f"Bearer {key}"},
        ) as judge_client,
    ):
        harness = MemoryHarness(harness_client, harness_url, limits)
        # An uncertain billed attempt is not automatically reusable. Keep the
        # marker on failure too; operators reconcile from the durable claim.
        marker = claim_path.with_name(claim_path.name + ".started")
        descriptor = os.open(marker, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.close(descriptor)
        report = await evaluate(
            assessment_id=claim.assessment_id,
            agent_id=claim.agent_id,
            artifact_sha256=claim.artifact_sha256,
            screened_image_sha256=claim.screened_image_sha256,
            bench_version=claim.bench_version,
            seed=claim.seed,
            harness=harness,
            examiner=AstraExaminer(judge_client, limits),
        )
    # Exclusive create prevents accidental overwrite/cherry-picking. Keep the
    # seed and lease token in the private claim file, not the report artifact.
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("x", encoding="utf-8") as handle:
        os.chmod(output, 0o600)
        handle.write(report.model_dump_json(indent=2) + "\n")
    if platform_url:
        async with httpx.AsyncClient(
            timeout=30,
            follow_redirects=False,
            trust_env=False,
            headers={"Authorization": f"Bearer {platform_token}"},
        ) as platform:
            response = await platform.post(
                platform_url.rstrip("/")
                + "/api/v1/admin/conversation-assessments/"
                + f"{claim.assessment_id}/result",
                json={
                    "lease_token": str(claim.lease_token),
                    "report": report.model_dump(mode="json"),
                },
            )
            if not response.is_success:
                raise RuntimeError("report saved locally; Platform did not accept it")
    return 0 if report.status == "completed" else 2


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--claim", type=Path, required=True)
    parser.add_argument("--harness-url", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--platform-url", help="Submit the saved result to this Platform HTTPS origin"
    )
    args = parser.parse_args()
    raise SystemExit(
        asyncio.run(run(args.claim, args.harness_url, args.output, args.platform_url))
    )


if __name__ == "__main__":
    main()
