#!/usr/bin/env python
"""Regenerate the committed wire-contract goldens.

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
import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType

sys.path.insert(0, str(Path(__file__).resolve().parent))

from screening_protocol_freshness import assert_fresh  # noqa: E402


def _load_contract_schema() -> ModuleType:
    """Load the subnet-owned schema helper without shadowing platform ``ditto``.

    The documented authoritative invocation runs this script from a platform
    checkout. Importing ``ditto.tests`` would then require the platform to ship
    subnet test helpers. Loading only the helper file keeps ``ditto.api_models``
    resolved from the active checkout, which is the contract source of truth.
    """
    schema_path = (
        Path(__file__).resolve().parent.parent
        / "ditto"
        / "tests"
        / "contract"
        / "_schema.py"
    )
    spec = importlib.util.spec_from_file_location(
        "validator_contract_schema", schema_path
    )
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load validator contract schema from {schema_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_CONTRACT_DIR = Path(__file__).resolve().parent.parent / "ditto" / "tests" / "contract"
_DEFAULT_OUT = _CONTRACT_DIR / "validator_contract.json"
_DEFAULT_MINER_OUT = _CONTRACT_DIR / "miner_contract.json"
_DEFAULT_CONFIRMATION_OUT = _CONTRACT_DIR / "confirmation_contract.json"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--out",
        type=Path,
        default=_DEFAULT_OUT,
        help="destination validator golden path (default: the committed one)",
    )
    parser.add_argument(
        "--confirmation-out",
        type=Path,
        default=_DEFAULT_CONFIRMATION_OUT,
        help="destination private v9 confirmation contract golden",
    )
    parser.add_argument(
        "--miner-out",
        type=Path,
        default=_DEFAULT_MINER_OUT,
        help="destination miner golden path (default: the committed one)",
    )
    args = parser.parse_args()
    # A stale built copy of ditto-screening-protocol makes every model below
    # describe an older contract, so the goldens this writes would be a
    # regression wearing a clean diff. Refuse rather than emit one.
    assert_fresh()
    schema = _load_contract_schema()
    for compute, out in (
        (schema.compute_contract, args.out),
        (schema.compute_miner_contract, args.miner_out),
        (schema.compute_confirmation_contract, args.confirmation_out),
    ):
        contract = compute()
        out.write_text(json.dumps(contract, indent=2, sort_keys=True) + "\n")
        print(f"wrote {len(contract)} model(s) to {out}")


if __name__ == "__main__":
    main()
