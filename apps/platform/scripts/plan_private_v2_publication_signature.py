"""Write the exact private v2 Hippius publication message for external signing."""

from __future__ import annotations

import sys
from argparse import ArgumentParser
from pathlib import Path

from ditto.api_server.coding_hippius_probe import (
    HippiusProbeReceiptError,
    load_hippius_probe_receipt,
    resolve_repository_source_sha,
)
from ditto.api_server.coding_hippius_publication import (
    HippiusPrivateInputPublicationError,
    load_curator_signing_public_key,
)
from ditto.api_server.coding_private_v2_publication import (
    private_v2_publication_signing_message,
    write_private_v2_publication_signing_message,
)
from ditto.api_server.coding_private_v2_transport import (
    PrivateV2TransportError,
    verify_private_v2_transport,
)

_REPOSITORY_ROOT = Path(__file__).resolve().parents[3]


def main(argv: list[str] | None = None) -> int:
    parser = ArgumentParser(description=__doc__)
    parser.add_argument("--transport", type=Path, required=True)
    parser.add_argument("--probe-receipt", type=Path, required=True)
    parser.add_argument("--curator-public-key", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        manifest = verify_private_v2_transport(args.transport)
        probe, probe_sha256 = load_hippius_probe_receipt(args.probe_receipt)
        _public_key, signing_key_sha256 = load_curator_signing_public_key(
            args.curator_public_key
        )
        source_sha = resolve_repository_source_sha(_REPOSITORY_ROOT)
        message = private_v2_publication_signing_message(
            manifest=manifest,
            source_sha=source_sha,
            probe_receipt_payload_sha256=probe_sha256,
            private_input_authority_sha256=(probe.private_input_authority_sha256),
            curator_signing_key_sha256=signing_key_sha256,
        )
        message_sha256 = write_private_v2_publication_signing_message(
            message=message,
            output=args.output,
        )
    except (
        HippiusProbeReceiptError,
        HippiusPrivateInputPublicationError,
        PrivateV2TransportError,
    ) as error:
        print(f"private v2 signing request failed: {error}", file=sys.stderr)
        return 2
    except Exception:
        print(
            "private v2 signing request failed: unexpected internal error",
            file=sys.stderr,
        )
        return 2
    print(f"private v2 signing request ready; message_sha256={message_sha256}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
