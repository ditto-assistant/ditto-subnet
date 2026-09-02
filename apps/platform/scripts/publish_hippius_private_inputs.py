"""Publish externally signed encrypted private inputs to Hippius."""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

from ditto.api_server.coding_hippius_encryption import (
    HippiusPrivateInputEncryptionError,
)
from ditto.api_server.coding_hippius_probe import (
    HippiusProbeReceiptError,
    resolve_repository_source_sha,
)
from ditto.api_server.coding_hippius_publication import (
    HIPPIUS_PRIVATE_INPUT_PUBLICATION_CONFIRMATION,
    AiobotoHippiusPrivateInputPublicationTransport,
    HippiusPrivateInputPublicationError,
    parse_hippius_private_input_publication_config,
    publish_hippius_private_inputs,
    write_hippius_private_input_publication_receipt,
)

_REPOSITORY_ROOT = Path(__file__).resolve().parents[3]


async def _run(
    *,
    transport_dir: Path,
    probe_receipt: Path,
    curator_public_key: Path,
    curator_signature: Path,
    receipt_output: Path,
) -> tuple[int, str]:
    config = parse_hippius_private_input_publication_config()
    source_sha = resolve_repository_source_sha(_REPOSITORY_ROOT)
    async with AiobotoHippiusPrivateInputPublicationTransport(config) as transport:
        receipt = await publish_hippius_private_inputs(
            config=config,
            transport=transport,
            transport_dir=transport_dir,
            probe_receipt_path=probe_receipt,
            curator_public_key_path=curator_public_key,
            curator_signature_path=curator_signature,
            source_sha=source_sha,
        )
    payload_sha256 = write_hippius_private_input_publication_receipt(
        receipt=receipt,
        output=receipt_output,
    )
    return len(receipt.objects), payload_sha256


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--transport-dir", type=Path, required=True)
    parser.add_argument("--probe-receipt", type=Path, required=True)
    parser.add_argument("--curator-public-key", type=Path, required=True)
    parser.add_argument("--curator-signature", type=Path, required=True)
    parser.add_argument("--receipt-output", type=Path, required=True)
    parser.add_argument(
        "--confirm",
        required=True,
        help=(f"must be exactly: {HIPPIUS_PRIVATE_INPUT_PUBLICATION_CONFIRMATION}"),
    )
    args = parser.parse_args(argv)
    if args.confirm != HIPPIUS_PRIVATE_INPUT_PUBLICATION_CONFIRMATION:
        parser.error(
            "--confirm must be exactly: "
            f"{HIPPIUS_PRIVATE_INPUT_PUBLICATION_CONFIRMATION}"
        )
    try:
        count, receipt_sha256 = asyncio.run(
            _run(
                transport_dir=args.transport_dir,
                probe_receipt=args.probe_receipt,
                curator_public_key=args.curator_public_key,
                curator_signature=args.curator_signature,
                receipt_output=args.receipt_output,
            )
        )
    except (
        HippiusPrivateInputEncryptionError,
        HippiusPrivateInputPublicationError,
        HippiusProbeReceiptError,
    ) as error:
        print(f"Hippius private-input publication failed: {error}", file=sys.stderr)
        return 2
    except Exception:
        print(
            "Hippius private-input publication failed: unexpected internal error",
            file=sys.stderr,
        )
        return 2
    print(
        f"published {count} encrypted private inputs; "
        f"receipt_payload_sha256={receipt_sha256}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
