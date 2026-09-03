"""Write the exact private-input publication message for external signing."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from ditto.api_server.coding_hippius_encryption import (
    HippiusPrivateInputEncryptionError,
    load_hippius_private_input_transport,
)
from ditto.api_server.coding_hippius_probe import (
    HippiusProbeReceiptError,
    load_hippius_probe_receipt,
)
from ditto.api_server.coding_hippius_publication import (
    HippiusPrivateInputPublicationError,
    hippius_private_input_signing_message,
    load_curator_signing_public_key,
    write_hippius_private_input_signing_message,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--transport-dir", type=Path, required=True)
    parser.add_argument("--probe-receipt", type=Path, required=True)
    parser.add_argument("--curator-public-key", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        manifest = load_hippius_private_input_transport(args.transport_dir)
        probe_receipt, probe_payload_sha256 = load_hippius_probe_receipt(
            args.probe_receipt
        )
        _public_key, signing_key_sha256 = load_curator_signing_public_key(
            args.curator_public_key
        )
        message = hippius_private_input_signing_message(
            manifest=manifest,
            probe_receipt_payload_sha256=probe_payload_sha256,
            private_input_authority_sha256=(
                probe_receipt.private_input_authority_sha256
            ),
            curator_signing_key_sha256=signing_key_sha256,
        )
        message_sha256 = write_hippius_private_input_signing_message(
            message=message,
            output=args.output,
        )
    except (
        HippiusPrivateInputEncryptionError,
        HippiusPrivateInputPublicationError,
        HippiusProbeReceiptError,
    ) as error:
        print(f"Hippius signing request failed: {error}", file=sys.stderr)
        return 2
    except Exception:
        print(
            "Hippius signing request failed: unexpected internal error",
            file=sys.stderr,
        )
        return 2
    print(f"Hippius signing request ready; message_sha256={message_sha256}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
