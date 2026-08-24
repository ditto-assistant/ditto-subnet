"""Verify an external private Coding catalog and emit its upload plan."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from ditto.api_server.coding_catalog_publication import (
    CodingCatalogPublicationError,
    plan_private_catalog_publication,
    write_private_catalog_publication_plan,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--commitment", type=Path, required=True)
    parser.add_argument("--records-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        plan = plan_private_catalog_publication(
            commitment_path=args.commitment,
            records_dir=args.records_dir,
        )
        write_private_catalog_publication_plan(plan=plan, output=args.output)
    except CodingCatalogPublicationError as error:
        print(f"private catalog publication preflight failed: {error}", file=sys.stderr)
        return 2
    print(
        f"verified {len(plan.objects)} private catalog records; "
        f"publication_sha256={plan.publication_sha256}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
