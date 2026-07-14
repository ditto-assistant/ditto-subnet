#!/usr/bin/env python
"""Regenerate the platform-authored screener-heartbeat contract golden."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from ditto.tests.contract._schema import (
    SHARED_SCREENER_HEARTBEAT_MODELS,
    compute_contract,
)

_DEFAULT_OUT = (
    Path(__file__).resolve().parent.parent
    / "ditto"
    / "tests"
    / "contract"
    / "screener_heartbeat_contract.json"
)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=_DEFAULT_OUT)
    args = parser.parse_args()
    contract = compute_contract(
        models=SHARED_SCREENER_HEARTBEAT_MODELS,
        module="ditto.api_models.screener",
    )
    args.out.write_text(json.dumps(contract, indent=2, sort_keys=True) + "\n")
    print(f"wrote {len(contract)} model(s) to {args.out}")


if __name__ == "__main__":
    main()
