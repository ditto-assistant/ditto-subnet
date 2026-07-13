"""Shared fixtures for the validator worker tests."""

from __future__ import annotations

import logging

import pytest


@pytest.fixture(autouse=True)
def _restore_ditto_log_propagation() -> object:
    """Keep ``caplog`` able to capture ``ditto.*`` records regardless of order.

    ``_apply_ditto_logging`` (the worker entrypoint) sets ``propagate = False`` on
    the ``ditto`` logger so its lines are not double-emitted in production. Once
    any test exercises that path, later tests that assert on log records via
    ``caplog`` (which reads through the root logger) capture nothing. Force
    propagation on around each test and restore whatever was there after.
    """
    ditto = logging.getLogger("ditto")
    saved_propagate = ditto.propagate
    saved_level = ditto.level
    ditto.propagate = True
    ditto.setLevel(logging.NOTSET)
    # An earlier test that ran the entrypoint may have clamped child levels above
    # WARNING; reset them so a child's own level does not filter records before
    # they can propagate to caplog's root handler.
    for name, child in logging.Logger.manager.loggerDict.items():
        if name.startswith("ditto.") and isinstance(child, logging.Logger):
            child.setLevel(logging.NOTSET)
    try:
        yield
    finally:
        ditto.propagate = saved_propagate
        ditto.setLevel(saved_level)
