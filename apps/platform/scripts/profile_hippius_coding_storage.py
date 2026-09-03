"""Derive one expiry-bound Hippius Coding provider profile from a probe."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from ditto.api_server.coding_hippius_probe import (
    HippiusProbeReceiptError,
    build_hippius_provider_profile,
    load_hippius_probe_receipt,
    write_hippius_provider_profile,
)

HIPPIUS_PROVIDER_PROFILE_CONFIRMATION = "PROFILE HIPPIUS CODING STORAGE"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--probe-receipt", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--confirm", required=True)
    args = parser.parse_args(argv)
    if args.confirm != HIPPIUS_PROVIDER_PROFILE_CONFIRMATION:
        parser.error(
            "--confirm must be exactly: " + HIPPIUS_PROVIDER_PROFILE_CONFIRMATION
        )
    try:
        receipt, receipt_payload_sha256 = load_hippius_probe_receipt(args.probe_receipt)
        profile = build_hippius_provider_profile(
            receipt=receipt,
            probe_receipt_payload_sha256=receipt_payload_sha256,
        )
        profile_payload_sha256 = write_hippius_provider_profile(
            profile=profile,
            output=args.output,
        )
    except HippiusProbeReceiptError as error:
        print(f"Hippius provider profile failed: {error}", file=sys.stderr)
        return 2
    except Exception:
        print(
            "Hippius provider profile failed: unexpected internal error",
            file=sys.stderr,
        )
        return 2
    print(
        "Hippius provider profile ready; "
        f"profile_payload_sha256={profile_payload_sha256}"
    )
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
