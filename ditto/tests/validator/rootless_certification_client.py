"""CI-only validator client for the rootless certification service probe.

The rootless Docker CI job's Go integration test runs this with a synthetic
bearer (from ``DITTOBENCH_CERT_IT_TOKEN``) against a real certification socket.
It uses the validator's production runtime and socket transport, pinned to a
temporary socket path because the fixed ``/run`` path is not writable on a CI
runner. It prints only the outcome, never the bearer.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from types import SimpleNamespace

from ditto.validator.coding_canary_runtime import CodingCanaryRuntime
from ditto.validator.coding_certification_socket import (
    CertificationSocketIdentity,
    CertificationSocketTransport,
)
from ditto.validator.errors import PlatformInfrastructureError


async def _probe(arguments: argparse.Namespace) -> str:
    identity = CertificationSocketIdentity(
        uid=arguments.uid, gid=arguments.gid, path=arguments.socket
    )
    config = SimpleNamespace(
        dittobench_control_token="",
        coding_certification_control_token=os.environ["DITTOBENCH_CERT_IT_TOKEN"],
        coding_certification_socket_uid=arguments.uid,
        coding_certification_socket_gid=arguments.gid,
        coding_certification_runtime_image_digest=arguments.image_digest,
        coding_certification_pack_manifest_sha256=arguments.manifest_sha256,
    )
    runtime = CodingCanaryRuntime(
        config,  # type: ignore[arg-type]
        transport=CertificationSocketTransport(identity),
    )
    try:
        readiness = await runtime.require_ready()
    except PlatformInfrastructureError as error:
        message = str(error)
        if "socket was refused" in message:
            return "socket_refused"
        marker = "failure="
        if marker in message:
            return "not_ready:" + message.split(marker, 1)[1].rstrip(")")
        return "error"
    finally:
        await runtime.aclose()
    if readiness.canary_manifest_sha256 != arguments.manifest_sha256:
        return "error"
    return "ready"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--socket", required=True)
    parser.add_argument("--uid", type=int, required=True)
    parser.add_argument("--gid", type=int, required=True)
    parser.add_argument("--image-digest", required=True)
    parser.add_argument("--manifest-sha256", required=True)
    parser.add_argument("--expect", required=True)
    arguments = parser.parse_args()
    outcome = asyncio.run(_probe(arguments))
    print(f"outcome={outcome} expected={arguments.expect}")
    return 0 if outcome == arguments.expect else 1


if __name__ == "__main__":
    sys.exit(main())
