"""Python twin of ``go/cmd/parity-smoke``.

Prints a deterministic JSON envelope describing the outputs of the three
antigaming primitives (:func:`partition_fixture`, :func:`paraphrase_seed`,
:func:`distractor_bundle_for`) for a fixed input set. CI diffs this output
against the Go reference; any drift fails the build before it can reach a
validator in production.

The input set is inlined here and in ``go/cmd/parity-smoke/main.go``. Any
change MUST be applied to both files at once and validated locally with
``make test`` (which now wraps the smoke).
"""

from __future__ import annotations

import json
import sys

from ditto.bench.runner.antigaming import (
    distractor_bundle_for,
    paraphrase_seed,
    partition_fixture,
)

_CASE_IDS = [
    "a",
    "b",
    "c",
    "d",
    "e",
    "f",
    "g",
    "h",
    "i",
    "j",
    "k",
    "l",
    "m",
    "n",
    "o",
    "p",
    "q",
    "r",
    "s",
    "t",
]

_CANDIDATES = [f"pair-{i:02d}" for i in range(1, 21)]

_PARTITION_SECRET = "parity-smoke"
_PARAPHRASE_SECRET = "parity-smoke"
_DISTRACTOR_SECRET = "parity-smoke"
_DISTRACTOR_CASE = "case-1"
_DISTRACTOR_BUNDLE_N = 5
_PARTITION_PRIV_FRAC = 0.25
_PARTITION_CANRY_FRAC = 0.15


def main() -> int:
    """Emit the parity envelope as pretty-printed JSON on stdout."""
    hidden = partition_fixture(
        _CASE_IDS,
        _PARTITION_SECRET,
        private_frac=_PARTITION_PRIV_FRAC,
        canary_frac=_PARTITION_CANRY_FRAC,
    )

    payload = {
        "partition_fixture": {
            "public": list(hidden.public),
            "private": list(hidden.private),
            "canary": list(hidden.canary),
        },
        "paraphrase_seed": {
            case_id: paraphrase_seed(_PARAPHRASE_SECRET, case_id)
            for case_id in _CASE_IDS
        },
        "distractor_bundle": distractor_bundle_for(
            _DISTRACTOR_CASE,
            ["pair-01", "pair-02"],
            ["pair-19"],
            _CANDIDATES,
            _DISTRACTOR_SECRET,
            _DISTRACTOR_BUNDLE_N,
        ),
    }

    sys.stdout.write(json.dumps(payload, indent=2, ensure_ascii=False))
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
