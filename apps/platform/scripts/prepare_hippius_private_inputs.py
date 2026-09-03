"""Encrypt verified private Coding records for later Hippius publication."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from ditto.api_server.coding_catalog_publication import CodingCatalogPublicationError
from ditto.api_server.coding_hippius_encryption import (
    HIPPIUS_PRIVATE_INPUT_ENCRYPTION_CONFIRMATION,
    HippiusPrivateInputEncryptionError,
    prepare_hippius_private_input_transport,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--commitment", type=Path, required=True)
    parser.add_argument("--records-dir", type=Path, required=True)
    parser.add_argument("--wrapping-public-key", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument(
        "--confirm",
        required=True,
        help=(f"must be exactly: {HIPPIUS_PRIVATE_INPUT_ENCRYPTION_CONFIRMATION}"),
    )
    args = parser.parse_args(argv)
    if args.confirm != HIPPIUS_PRIVATE_INPUT_ENCRYPTION_CONFIRMATION:
        parser.error(
            "--confirm must be exactly: "
            f"{HIPPIUS_PRIVATE_INPUT_ENCRYPTION_CONFIRMATION}"
        )
    try:
        manifest = prepare_hippius_private_input_transport(
            commitment_path=args.commitment,
            records_dir=args.records_dir,
            wrapping_public_key_path=args.wrapping_public_key,
            output_dir=args.output_dir,
        )
    except (CodingCatalogPublicationError, HippiusPrivateInputEncryptionError) as error:
        print(f"Hippius private-input preparation failed: {error}", file=sys.stderr)
        return 2
    except Exception:
        print(
            "Hippius private-input preparation failed: unexpected internal error",
            file=sys.stderr,
        )
        return 2
    print(
        f"encrypted {len(manifest.objects)} private catalog records; "
        f"transport_manifest_sha256={manifest.transport_manifest_sha256}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
