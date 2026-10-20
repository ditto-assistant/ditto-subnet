"""Emit a read-only, bounded two-route quote from a reviewed JSON snapshot."""

import argparse
import json
from dataclasses import asdict
from pathlib import Path

from ditto.treasury.ledger import append
from ditto.treasury.quote import Pool, quote_topup


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("snapshot", type=Path, help="file with finalized pool reserves")
    parser.add_argument("--ledger", type=Path, required=True)
    args = parser.parse_args()
    raw = json.loads(args.snapshot.read_text())
    quote = quote_topup(
        Pool(**raw["ditto"]),
        Pool(**raw["gm"]),
        source_alpha_rao=raw["source_alpha_rao"],
        max_source_alpha_rao=raw["max_source_alpha_rao"],
        max_impact_bps=raw["max_impact_bps"],
    )
    output = asdict(quote)
    append(args.ledger, event="dry_run", payload=output)
    print(json.dumps(output, sort_keys=True))


if __name__ == "__main__":
    main()
