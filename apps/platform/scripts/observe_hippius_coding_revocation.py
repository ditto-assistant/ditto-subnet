"""Observe revocation delay for one disposable Hippius Coding S3 sub-token.

The target token must be a pre-created, private-input read-only sub-token scoped
to ``coding-revocation-probe/v1/``.  This command is confirmation-gated,
default-off, and writes only a redacted local receipt.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

from ditto.api_server.coding_hippius_probe import (
    AiobotoHippiusProbeTransport,
    HippiusCredentialRole,
    HippiusProbeConfig,
    HippiusProbeCredential,
    HippiusProbeError,
    parse_hippius_probe_config,
    resolve_repository_source_sha,
)
from ditto.api_server.coding_hippius_revocation import (
    HIPPIUS_REVOCATION_CONFIRMATION,
    HippiusRevocationManagementConfig,
    HippiusRevocationTarget,
    HttpxHippiusRevocationManagement,
    run_hippius_revocation_observation,
    write_hippius_revocation_receipt,
)


class _LiveStorage:
    def __init__(self, config: HippiusProbeConfig) -> None:
        self._config = config
        self._transport = AiobotoHippiusProbeTransport(config)

    async def __aenter__(self) -> _LiveStorage:
        await self._transport.__aenter__()
        return self

    async def __aexit__(self, *args: object) -> None:
        await self._transport.__aexit__(*args)

    async def put_synthetic(self, *, bucket: str, key: str, body: bytes) -> None:
        await self._transport.put_object(
            role=HippiusCredentialRole.PRIVATE_INPUT_CURATOR,
            bucket=bucket,
            key=key,
            body=body,
            metadata={"probe-kind": "revocation"},
        )

    async def get_as_target(
        self, *, target: HippiusRevocationTarget, bucket: str, key: str
    ) -> bytes:
        temporary = HippiusProbeConfig(
            endpoint_url=self._config.endpoint_url,
            private_input_bucket=self._config.private_input_bucket,
            sealed_evidence_bucket=self._config.sealed_evidence_bucket,
            private_input_curator=self._config.private_input_curator,
            private_input_reader=target.credential,
            evidence_mediator=self._config.evidence_mediator,
            region=self._config.region,
            timeout_seconds=self._config.timeout_seconds,
        )
        async with AiobotoHippiusProbeTransport(temporary) as target_transport:
            return await target_transport.get_object(
                role=HippiusCredentialRole.PRIVATE_INPUT_READER,
                bucket=bucket,
                key=key,
                max_bytes=4097,
            )

    async def delete_synthetic(self, *, bucket: str, key: str) -> None:
        await self._transport.delete_object(
            role=HippiusCredentialRole.PRIVATE_INPUT_CURATOR,
            bucket=bucket,
            key=key,
        )


def _required_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise ValueError(f"{name} must be configured")
    return value


async def _run(args: argparse.Namespace) -> str:
    config = parse_hippius_probe_config(os.environ)
    target = HippiusRevocationTarget(
        token_id=args.target_token_id,
        credential=HippiusProbeCredential(
            access_key=_required_env("DITTO_CODING_HIPPIUS_REVOCATION_ACCESS_KEY"),
            secret_key=_required_env("DITTO_CODING_HIPPIUS_REVOCATION_SECRET_KEY"),
        ),
    )
    management = HttpxHippiusRevocationManagement(
        HippiusRevocationManagementConfig(
            access_token=_required_env("DITTO_CODING_HIPPIUS_MANAGEMENT_TOKEN")
        )
    )
    async with _LiveStorage(config) as storage:
        receipt = await run_hippius_revocation_observation(
            config=config,
            target=target,
            storage=storage,
            management=management,
            source_sha=resolve_repository_source_sha(Path(__file__).parents[2]),
            provider_profile_payload_sha256=args.provider_profile_payload_sha256,
        )
    digest = write_hippius_revocation_receipt(receipt=receipt, output=args.output)
    return digest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target-token-id", required=True)
    parser.add_argument("--provider-profile-payload-sha256", required=True)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--confirm", required=True)
    args = parser.parse_args(argv)
    if args.confirm != HIPPIUS_REVOCATION_CONFIRMATION:
        parser.error("--confirm must be exactly: " + HIPPIUS_REVOCATION_CONFIRMATION)
    try:
        digest = asyncio.run(_run(args))
    except (HippiusProbeError, ValueError):
        print("Hippius revocation observation failed", file=sys.stderr)
        return 2
    except Exception:
        print(
            "Hippius revocation observation failed: unexpected internal error",
            file=sys.stderr,
        )
        return 2
    print(f"Hippius revocation observation ready; receipt_payload_sha256={digest}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
