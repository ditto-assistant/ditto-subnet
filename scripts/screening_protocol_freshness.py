"""Detect a stale installed copy of ``ditto-screening-protocol``.

``ditto-screening-protocol`` is a path dependency, and uv installs it as a
*built* copy rather than an editable one. After its source changes, a plain
``uv sync`` reports "Audited" and leaves the previously-built copy in place.
Nothing raises. The models simply describe an older contract, and every
consumer then lies in a different direction:

* the contract tests report that a golden "drifted from the platform contract"
  when the golden is correct and the *installed models* are the stale side, so
  the documented remedy (regenerate the golden) is exactly the wrong move;
* :mod:`scripts.gen_validator_contract` and
  ``apps/backroom/scripts/platform-contract/generate.sh`` write a **regressed**
  artifact that reads as an ordinary clean diff -- a v11 rollout regenerated
  ``bench_version: 9 | 10`` back down to ``9`` precisely this way, and the
  deletion was two lines buried in an otherwise additive change.

The deployment scripts already reinstall for this same reason
(``apps/platform/scripts/update.sh``, ``workers/screener/scripts/*.sh``); this
module is the equivalent guard for the paths that *generate* artifacts.

Deliberately conservative: it reports staleness only when it can positively
prove it, so it can add information to a failure that was going to happen
anyway but can never invent one.
"""

from __future__ import annotations

import hashlib
import importlib
from pathlib import Path

MONOREPO_ROOT = Path(__file__).resolve().parent.parent
PACKAGE = "ditto_screening_protocol"
SOURCE = MONOREPO_ROOT / "packages" / "ditto-screening-protocol" / PACKAGE

REMEDY = (
    "uv sync --reinstall-package ditto-screening-protocol "
    "(plain `uv sync` will NOT fix this -- it reports 'Audited' and keeps the "
    "stale built copy). Run it in the repository root AND in apps/platform, "
    "then re-run. Do NOT regenerate a golden or a client until this is clean: "
    "regenerating against a stale copy commits a silent regression."
)


def _tree_digest(directory: Path) -> str:
    """Hash a package's ``.py`` sources, path-sensitively and order-stably."""
    digest = hashlib.sha256()
    for path in sorted(directory.rglob("*.py")):
        digest.update(str(path.relative_to(directory)).encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()


def stale_install_message() -> str | None:
    """Return remediation text when the installed copy lags its source.

    ``None`` -- meaning "no proof of staleness" -- when the package is not
    importable, is installed editable (same directory as the source), or the
    monorepo source tree is not present. Those are all states where this check
    has nothing to say, and staying quiet keeps it from ever becoming the
    reason a green run turns red.
    """
    try:
        module = importlib.import_module(PACKAGE)
    except ImportError:
        return None
    origin = getattr(module, "__file__", None)
    if origin is None or not SOURCE.is_dir():
        return None
    installed = Path(origin).resolve().parent
    if installed == SOURCE or _tree_digest(installed) == _tree_digest(SOURCE):
        return None
    return f"installed {PACKAGE} at {installed} does not match {SOURCE}. {REMEDY}"


def assert_fresh() -> None:
    """Raise before writing any generated artifact from stale models."""
    message = stale_install_message()
    if message is not None:
        raise SystemExit(f"refusing to generate from a stale install: {message}")


def hint() -> str:
    """Return a trailing sentence for an assertion message, or ``''``."""
    message = stale_install_message()
    return f"\n\nNOTE: this failure is probably not your change -- {message}"
