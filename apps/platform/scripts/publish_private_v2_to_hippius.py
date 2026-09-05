"""Publish one externally signed private Coding v2 payload to Hippius."""

from __future__ import annotations

import asyncio
import sys
from argparse import ArgumentParser
from pathlib import Path

from ditto.api_server.coding_hippius_probe import (
    HippiusProbeReceiptError,
    resolve_repository_source_sha,
)
from ditto.api_server.coding_hippius_publication import (
    AiobotoHippiusPrivateInputPublicationTransport,
    HippiusPrivateInputPublicationError,
    parse_hippius_private_input_publication_config,
)
from ditto.api_server.coding_private_v2_publication import (
    PRIVATE_V2_PUBLICATION_CONFIRMATION,
    publish_private_v2_to_hippius,
    write_private_v2_publication_receipt,
)
from ditto.api_server.coding_private_v2_transport import PrivateV2TransportError

_REPOSITORY_ROOT = Path(__file__).resolve().parents[3]


async def _run(
    *,
    transport_directory: Path,
    probe_receipt: Path,
    curator_public_key: Path,
    curator_signature: Path,
    receipt_output: Path,
) -> tuple[int, str]:
    config = parse_hippius_private_input_publication_config()
    source_sha = resolve_repository_source_sha(_REPOSITORY_ROOT)
    async with AiobotoHippiusPrivateInputPublicationTransport(config) as transport:
        receipt = await publish_private_v2_to_hippius(
            config=config,
            transport=transport,
            transport_directory=transport_directory,
            probe_receipt_path=probe_receipt,
            curator_public_key_path=curator_public_key,
            curator_signature_path=curator_signature,
            source_sha=source_sha,
        )
    receipt_sha256 = write_private_v2_publication_receipt(
        receipt=receipt,
        output=receipt_output,
    )
    return receipt.object_count, receipt_sha256


def main(argv: list[str] | None = None) -> int:
    parser = ArgumentParser(description=__doc__)
    parser.add_argument("--transport", type=Path, required=True)
    parser.add_argument("--probe-receipt", type=Path, required=True)
    parser.add_argument("--curator-public-key", type=Path, required=True)
    parser.add_argument("--curator-signature", type=Path, required=True)
    parser.add_argument("--receipt-output", type=Path, required=True)
    parser.add_argument(
        "--confirm",
        required=True,
        help=f"must be exactly: {PRIVATE_V2_PUBLICATION_CONFIRMATION}",
    )
    args = parser.parse_args(argv)
    if args.confirm != PRIVATE_V2_PUBLICATION_CONFIRMATION:
        parser.error(
            f"--confirm must be exactly: {PRIVATE_V2_PUBLICATION_CONFIRMATION}"
        )
    try:
        object_count, receipt_sha256 = asyncio.run(
            _run(
                transport_directory=args.transport,
                probe_receipt=args.probe_receipt,
                curator_public_key=args.curator_public_key,
                curator_signature=args.curator_signature,
                receipt_output=args.receipt_output,
            )
        )
    except (
        HippiusProbeReceiptError,
        HippiusPrivateInputPublicationError,
        PrivateV2TransportError,
    ) as error:
        print(f"private v2 Hippius publication failed: {error}", file=sys.stderr)
        return 2
    except Exception:
        print(
            "private v2 Hippius publication failed: unexpected internal error",
            file=sys.stderr,
        )
        return 2
    print(
        f"published {object_count} encrypted private v2 objects; "
        f"receipt_payload_sha256={receipt_sha256}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
