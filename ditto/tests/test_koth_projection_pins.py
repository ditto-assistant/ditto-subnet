"""Guard: Platform's KOTH projection keeps the validator's consensus constants.

``apps/platform/ditto/api_server/koth.py`` re-implements the validator's frozen
emissions fold so the public leaderboard can explain it, and its header asks to
keep the constants "byte-for-byte aligned" with ``ditto/validator/config.py``.
The two trees are separate ``ditto`` packages and cannot import each other, so
nothing enforced that: a margin, share, or z edited on one side would make the
leaderboard crown a different agent than the validators pay, with every test
still green. Like ``test_bench_version_pins``, this reads source text so the
pull request that moves one side fails before anything is deployed.
"""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
VALIDATOR_CONFIG = ROOT / "ditto/validator/config.py"
VALIDATOR_WEIGHTS = ROOT / "ditto/validator/weights.py"
PLATFORM_KOTH = ROOT / "apps/platform/ditto/api_server/koth.py"
PLATFORM_SCORES = ROOT / "apps/platform/ditto/db/queries/scores.py"


def _literal_constants(path: Path) -> dict[str, object]:
    """Module-level ``NAME = <literal>`` assignments; derived values are skipped."""
    constants: dict[str, object] = {}
    for node in ast.parse(path.read_text(), filename=str(path)).body:
        if not (
            isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
        ):
            continue
        try:
            constants[node.targets[0].id] = ast.literal_eval(node.value)
        except ValueError:
            continue
    return constants


def test_platform_koth_constants_match_the_validator_fold() -> None:
    validator = _literal_constants(VALIDATOR_CONFIG)
    platform = {
        name: value
        for name, value in _literal_constants(PLATFORM_KOTH).items()
        if name.startswith("KOTH_")
    }

    # The projection must mirror the fold's core knobs, not an empty subset.
    assert {
        "KOTH_MARGIN",
        "KOTH_RANK_SHARES",
        "KOTH_DETHRONE_Z",
        "KOTH_TAIL_SIZE",
    } <= platform.keys()
    missing = sorted(platform.keys() - validator.keys())
    assert not missing, f"Platform KOTH constants absent from the validator: {missing}"
    drifted = {
        name: (value, validator[name])
        for name, value in platform.items()
        if validator[name] != value
    }
    assert not drifted, f"Platform/validator KOTH constants differ: {drifted}"


def test_platform_eligibility_floor_matches_the_validator() -> None:
    assert (
        _literal_constants(PLATFORM_SCORES)["MIN_ELIGIBLE_CASES"]
        == _literal_constants(VALIDATOR_WEIGHTS)["MIN_ELIGIBLE_CASES"]
    )
