"""Explicit private native-v2 custody service and credential-free worker proxy."""

from __future__ import annotations

import argparse
import asyncio
import signal
import sys
from pathlib import Path


async def serve(config_path: Path) -> None:
    from uuid import UUID

    from ditto.api_models.coding_private_v2_registry import (
        CodingPrivateV2RegistrationAuthority,
    )
    from ditto.api_server.coding_hosted_private_grants import HostedPrivateGrantStore
    from ditto.api_server.coding_hosted_runtime_config import postgres_config
    from ditto.api_server.coding_hosted_runtime_io import read_json, read_private
    from ditto.api_server.coding_private_v2_custody import (
        PrivateV2Custody,
        ProtectedRSAKeyBackend,
    )
    from ditto.api_server.coding_private_v2_custody_socket import PrivateV2CustodyServer
    from ditto.api_server.coding_private_v2_retrieval import PrivateV2InputAuthority
    from ditto.db.factory import create_db_engine, create_session_maker

    config = read_json(config_path)
    if (
        not isinstance(config, dict)
        or config.get("schema") != "dittobench-coding-private-v2-custody-config-v1"
        or config.get("shadow_only") is not True
        or config.get("weight_eligible") is not False
    ):
        raise ValueError("configuration")
    registration = CodingPrivateV2RegistrationAuthority.model_validate(
        read_json(Path(config["registration_file"]))
    )
    paths = {
        name: Path(config[name])
        for name in (
            "transport_manifest",
            "payload_authority",
            "publication_receipt",
            "curator_public_key",
        )
    }
    for name, path in paths.items():
        read_private(
            path,
            65536 if name == "curator_public_key" else (16 << 20),
        )
    database, _ = postgres_config(
        read_json(Path(config["postgres_environment_file"]), 128 << 10)
    )
    worker = UUID(config["worker_id"])
    backend = ProtectedRSAKeyBackend(Path(config["private_key_file"]))
    engine = create_db_engine(database)
    server = None
    try:
        sessions = create_session_maker(engine)
        services = {}
        for audience in ("platform-authoring", "platform-grading"):
            authority = PrivateV2InputAuthority(
                registration=registration,
                transport_manifest=paths["transport_manifest"],
                payload_authority=paths["payload_authority"],
                publication_receipt=paths["publication_receipt"],
                trusted_curator_public_key_path=paths["curator_public_key"],
                reader_authority_sha256=config["reader_authority_sha256"],
                audience=audience,
            )
            authority.describe_selection(0)
            services[audience] = PrivateV2Custody(
                authority=authority,
                grants=HostedPrivateGrantStore(
                    sessions=sessions, worker_id=worker, audience=audience
                ),
                backend=backend,
            )
        server = PrivateV2CustodyServer(
            path=Path(config["socket_path"]),
            client_uid=config["client_uid"],
            services=services,
        )
        await server.start()
        stopped = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, stopped.set)
        try:
            await stopped.wait()
        finally:
            for sig in (signal.SIGTERM, signal.SIGINT):
                loop.remove_signal_handler(sig)
    finally:
        # Never dispose the database behind a still-active unwrap request.
        if server is not None:
            await server.close()
        await engine.dispose()


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Private native-v2 custody; no default activation"
    )
    modes = parser.add_mutually_exclusive_group(required=True)
    modes.add_argument("--serve-custody", action="store_true")
    modes.add_argument("--proxy-once", action="store_true")
    parser.add_argument("--config", type=Path)
    parser.add_argument("--socket", type=Path)
    parser.add_argument("--custodian-uid", type=int)
    args = parser.parse_args()
    if args.serve_custody:
        if (
            args.config is None
            or args.socket is not None
            or args.custodian_uid is not None
        ):
            parser.error("server requires only a protected configuration")
    elif args.config is not None or args.socket is None or args.custodian_uid is None:
        parser.error("proxy requires socket and expected custodian UID")
    try:
        if args.serve_custody:
            assert isinstance(args.config, Path)
            asyncio.run(serve(args.config))
        else:
            from ditto.api_server.coding_private_v2_custody_socket import proxy_request

            body = sys.stdin.buffer.readline(16385)
            assert isinstance(args.socket, Path) and isinstance(args.custodian_uid, int)
            result = asyncio.run(
                proxy_request(
                    path=args.socket, expected_server_uid=args.custodian_uid, body=body
                )
            )
            sys.stdout.buffer.write(result)
            sys.stdout.buffer.flush()
        return 0
    except Exception:
        print("native v2 custody unavailable", file=sys.stderr)
        return 70


if __name__ == "__main__":
    raise SystemExit(main())
