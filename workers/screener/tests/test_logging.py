"""The screener's INFO logs survive bittensor's WARNING clamp."""

from __future__ import annotations

import logging
from pathlib import Path

from ditto_screener.__main__ import _PACKAGE_ROOT, _apply_ditto_logging


def test_apply_ditto_logging_unclamps_the_screener_tree() -> None:
    # Per-module loggers exist before bittensor initialises (they are created by
    # the module-level ``getLogger(__name__)`` calls at import time)...
    worker_logger = logging.getLogger(f"{_PACKAGE_ROOT}.worker")
    gate_logger = logging.getLogger(f"{_PACKAGE_ROOT}.gate")
    # ...then bittensor's lazy init (during keypair load) clamps every existing
    # logger to WARNING, which would hide sweep and per-agent verdict lines.
    worker_logger.setLevel(logging.WARNING)
    gate_logger.setLevel(logging.WARNING)

    try:
        _apply_ditto_logging()

        assert worker_logger.isEnabledFor(logging.INFO)
        assert gate_logger.isEnabledFor(logging.INFO)
    finally:
        worker_logger.setLevel(logging.NOTSET)
        gate_logger.setLevel(logging.NOTSET)


def test_package_root_is_the_extracted_package_not_the_validator() -> None:
    # A bare ``ditto`` here would silently no-op against the ``ditto_screener.*``
    # tree that actually gets clamped.
    assert _PACKAGE_ROOT == "ditto_screener"


def test_package_root_resolves_under_python_m_execution() -> None:
    """``python -m ditto_screener`` runs __main__.py with ``__name__`` set to
    ``"__main__"``; the package root must still resolve to ``ditto_screener``
    (deriving it from ``__name__`` instead of ``__package__`` regresses to
    ``"__main__"`` and the logging un-clamp silently misses its own tree).
    """
    import importlib.util

    spec = importlib.util.find_spec("ditto_screener.__main__")
    assert spec is not None and spec.origin is not None
    source = Path(spec.origin).read_text()
    # Exec only the module body above the ``if __name__ == "__main__"`` guard so
    # ``main()`` (which needs runtime config) never fires, under the exact
    # globals ``python -m ditto_screener`` would supply.
    body = source.split("\nif __name__ ==")[0]
    namespace: dict[str, object] = {
        "__name__": "__main__",
        "__package__": "ditto_screener",
        "__file__": spec.origin,
    }
    exec(compile(body, spec.origin, "exec"), namespace)

    assert namespace["_PACKAGE_ROOT"] == "ditto_screener"
