#!/usr/bin/env python
"""Regenerate the committed wire-contract goldens.

The goldens under ``ditto/tests/contract/`` are the structural shape of the
wire models *as defined by Platform* — the source of truth, since Platform's
OpenAPI schema is the contract and the validator/miner client keeps a
hand-maintained copy of the models rather than importing a shared package.

This is the **only** generator for those goldens. ``apps/platform`` used to
carry a second, leaner copy that wrote the mirror alone: it silently skipped
``miner_contract.json`` and had no staleness guard, so it could regenerate a
regression wearing a clean diff. Both trees are now written from here, in one
run, from one set of models.

Because Platform and the subnet client expose the models at the same import
path (``ditto.api_models.*``), this generates from whichever ``ditto`` package
is importable, so it must be run with **Platform** on the path::

    cd apps/platform
    uv sync --reinstall-package ditto-screening-protocol
    uv run python ../../scripts/gen_validator_contract.py

That writes the subnet goldens and mirrors the validator + confirmation
goldens into ``apps/platform/ditto/tests/contract/``, which
``test_monorepo_validator_goldens_match_platform_byte_for_byte`` requires to
be byte-identical. Producing both from one computation is the point: the
mirror cannot drift from the artifact it mirrors.

Running this from the repository root instead regenerates from the subnet's
*copy* of the models — useful only to confirm the copy is self-consistent, so
the mirror is refused in that case rather than laundering an unreviewed client
change into the authoritative golden.
"""

from __future__ import annotations

import argparse
import importlib
import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType

sys.path.insert(0, str(Path(__file__).resolve().parent))

from screening_protocol_freshness import assert_fresh  # noqa: E402

_MONOREPO_ROOT = Path(__file__).resolve().parent.parent
_CONTRACT_DIR = _MONOREPO_ROOT / "ditto" / "tests" / "contract"
_PLATFORM_CONTRACT_DIR = (
    _MONOREPO_ROOT / "apps" / "platform" / "ditto" / "tests" / "contract"
)

_DEFAULT_OUT = _CONTRACT_DIR / "validator_contract.json"
_DEFAULT_MINER_OUT = _CONTRACT_DIR / "miner_contract.json"
_DEFAULT_CONFIRMATION_OUT = _CONTRACT_DIR / "confirmation_contract.json"


def _load_contract_schema() -> ModuleType:
    """Load the subnet-owned schema helper without shadowing platform ``ditto``.

    The documented authoritative invocation runs this script from a platform
    checkout. Importing ``ditto.tests`` would then require the platform to ship
    subnet test helpers. Loading only the helper file keeps ``ditto.api_models``
    resolved from the active checkout, which is the contract source of truth.
    """
    schema_path = _CONTRACT_DIR / "_schema.py"
    spec = importlib.util.spec_from_file_location(
        "validator_contract_schema", schema_path
    )
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load validator contract schema from {schema_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _models_are_the_subnet_copy() -> bool:
    """Report whether ``ditto`` resolved to this repo's client copy.

    Conservative in the same direction as the freshness guard: it answers
    ``True`` only when it can positively place the imported models inside the
    subnet tree. Anything else — ``apps/platform`` on the path, or a separate
    Platform checkout — is treated as authoritative, because refusing a valid
    regeneration is the more expensive mistake.
    """
    module = importlib.import_module("ditto.api_models.validator")
    origin = getattr(module, "__file__", None)
    if origin is None:
        return False
    return _MONOREPO_ROOT / "ditto" in Path(origin).resolve().parents


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
    parser.add_argument(
        "--mirror-dir",
        type=Path,
        default=_PLATFORM_CONTRACT_DIR,
        help=(
            "directory receiving the byte-identical Platform-side copies of the "
            "validator and confirmation goldens (default: the committed mirror)"
        ),
    )
    parser.add_argument(
        "--no-mirror",
        action="store_true",
        help="write only the subnet goldens and skip the Platform-side mirror",
    )
    args = parser.parse_args()
    # A stale built copy of ditto-screening-protocol makes every model below
    # describe an older contract, so the goldens this writes would be a
    # regression wearing a clean diff. Refuse rather than emit one.
    assert_fresh()
    schema = _load_contract_schema()

    mirror_dir: Path | None = None if args.no_mirror else args.mirror_dir
    if mirror_dir is not None and _models_are_the_subnet_copy():
        # Mirroring here would copy the subnet client's own shape over the
        # golden that exists to catch the subnet client drifting, which turns
        # the guard green on exactly the change it is meant to fail.
        print(
            "NOT mirroring to Platform: `ditto` resolved to this repo's client "
            "copy, which is not the contract source of truth. Re-run from "
            "apps/platform to refresh the mirror, or pass --no-mirror to "
            "silence this.",
            file=sys.stderr,
        )
        mirror_dir = None

    # Mirrored goldens are written from one computation, not regenerated twice,
    # so the byte-for-byte guard cannot fail on a difference in how they were
    # produced.
    plan: list[tuple[str, Path, list[Path]]] = [
        ("validator", args.out, []),
        ("confirmation", args.confirmation_out, []),
        ("miner", args.miner_out, []),
    ]
    if mirror_dir is not None:
        plan[0][2].append(mirror_dir / "validator_contract.json")
        plan[1][2].append(mirror_dir / "confirmation_contract.json")

    compute = {
        "validator": schema.compute_contract,
        "confirmation": schema.compute_confirmation_contract,
        "miner": schema.compute_miner_contract,
    }
    for kind, out, mirrors in plan:
        contract = compute[kind]()
        payload = json.dumps(contract, indent=2, sort_keys=True) + "\n"
        for destination in (out, *mirrors):
            destination.write_text(payload)
            print(f"wrote {len(contract)} model(s) to {destination}")


if __name__ == "__main__":
    main()
