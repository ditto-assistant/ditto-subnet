#!/usr/bin/env python
"""Regenerate the committed validator contract golden.

The golden (``ditto/tests/contract/validator_contract.json``) is the structural
shape of the validator wire models *as defined by the platform* — the source of
truth, since the platform's OpenAPI schema is the contract and there is no
shared package.

Because both repos expose the models at the same import path
(``ditto.api_models.validator``), this script generates the golden from whatever
``ditto`` package is importable. To refresh it from the platform after an
intentional contract change, run it with a **ditto-platform** checkout on the
path, e.g.::

    # from a ditto-platform checkout on the matching branch:
    uv run python /path/to/ditto-subnet/scripts/gen_validator_contract.py \
        --out /path/to/ditto-subnet/ditto/tests/contract/validator_contract.json

Running it inside ditto-subnet regenerates from this repo's *copy* — useful only
to confirm the copy is self-consistent, not to authoritatively refresh.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from ditto.tests.contract._schema import compute_confirmation_contract, compute_contract

_DEFAULT_OUT = (
    Path(__file__).resolve().parent.parent
    / "ditto"
    / "tests"
    / "contract"
    / "validator_contract.json"
)
_DEFAULT_CONFIRMATION_OUT = _DEFAULT_OUT.with_name("confirmation_contract.json")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--out",
        type=Path,
        default=_DEFAULT_OUT,
        help="destination golden path (default: the committed subnet golden)",
    )
    parser.add_argument(
        "--confirmation-out",
        type=Path,
        default=_DEFAULT_CONFIRMATION_OUT,
        help="destination private v9 confirmation contract golden",
    )
    args = parser.parse_args()
    contract = compute_contract()
    args.out.write_text(json.dumps(contract, indent=2, sort_keys=True) + "\n")
    print(f"wrote {len(contract)} model(s) to {args.out}")
    confirmation = compute_confirmation_contract()
    args.confirmation_out.write_text(
        json.dumps(confirmation, indent=2, sort_keys=True) + "\n"
    )
    print(f"wrote {len(confirmation)} model(s) to {args.confirmation_out}")


if __name__ == "__main__":
    main()
