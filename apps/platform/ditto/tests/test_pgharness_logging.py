"""The in-process template migration must leave the worker's logging alone.

``alembic/env.py`` calls ``fileConfig`` for the CLI. Run in-process by the
harness, that call disabled every ``ditto.*`` logger already imported, so the
xdist worker that built the template silently dropped the warnings its later
``caplog`` tests assert on. Offline mode runs the same ``env.py`` without a
database.
"""

from __future__ import annotations

import io
import logging
from collections.abc import Iterator

import pytest
from alembic.config import Config

from alembic import command
from ditto.tests import pgharness

_FIRST_REVISION = "93732e86fe02"
_SENTINEL = "ditto.tests.pgharness_logging_sentinel"


@pytest.fixture
def restored_logging(monkeypatch: pytest.MonkeyPatch) -> Iterator[logging.Logger]:
    """A pre-imported ``ditto.*`` logger, with global logging put back after."""
    for key, value in {
        "POSTGRES_USER": "offline",
        "POSTGRES_PASSWORD": "offline",
        "POSTGRES_DB": "offline",
    }.items():
        monkeypatch.setenv(key, value)
    root = logging.getLogger()
    handlers, level = list(root.handlers), root.level
    loggers = {
        name: logger.disabled
        for name, logger in logging.root.manager.loggerDict.items()
        if isinstance(logger, logging.Logger)
    }
    sentinel = logging.getLogger(_SENTINEL)
    try:
        yield sentinel
    finally:
        root.handlers[:] = handlers
        root.setLevel(level)
        for name, disabled in loggers.items():
            logging.getLogger(name).disabled = disabled
        sentinel.disabled = False


def _offline_upgrade(cfg: Config) -> None:
    cfg.output_buffer = io.StringIO()
    command.upgrade(cfg, _FIRST_REVISION, sql=True)


def test_harness_migration_keeps_imported_loggers_enabled(
    restored_logging: logging.Logger,
) -> None:
    handlers = list(logging.getLogger().handlers)

    _offline_upgrade(pgharness._alembic_config())

    assert restored_logging.disabled is False
    assert logging.getLogger().handlers == handlers


def test_cli_config_still_applies_alembic_ini_logging(
    restored_logging: logging.Logger,
) -> None:
    """Pins the hazard, so the test above cannot pass vacuously."""
    cfg = pgharness._alembic_config()
    del cfg.attributes["configure_logger"]

    _offline_upgrade(cfg)

    assert restored_logging.disabled is True
