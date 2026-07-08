"""Commit-reveal mode observability in the validator worker.

Under commit-reveal v3 the weight sink (``set_weights`` / Pylon) does the
timelock commit itself and the chain auto-reveals — the worker makes no separate
reveal call. ``_log_commit_reveal_mode`` only *observes* the mode so a cutover
can confirm commit-reveal is on, and is fail-open so a flaky read never wedges
weight-setting.
"""

from __future__ import annotations

import logging
from unittest.mock import AsyncMock, MagicMock

from ditto.validator.worker import ValidatorWorker


def _worker(*, require: bool, chain: MagicMock) -> ValidatorWorker:
    config = MagicMock()
    config.netuid = 3
    config.require_commit_reveal = require
    return ValidatorWorker(
        config=config,
        platform=MagicMock(),
        dittobench=MagicMock(),
        chain=chain,
        keypair=MagicMock(),
    )


_LOGGER = "ditto.validator.worker"


def _errors(caplog: object) -> list[logging.LogRecord]:
    return [r for r in caplog.records if r.levelno >= logging.ERROR]  # type: ignore[attr-defined]


class TestCommitRevealMode:
    async def test_logs_on_with_reveal_period_when_enabled(self, caplog) -> None:
        chain = MagicMock()
        chain.get_commit_reveal_enabled = AsyncMock(return_value=True)
        chain.get_reveal_period_epochs = AsyncMock(return_value=2)
        with caplog.at_level(logging.INFO, logger=_LOGGER):
            await _worker(require=True, chain=chain)._log_commit_reveal_mode()
        assert any(
            "commit-reveal ON" in r.getMessage()
            and "reveal period 2 epochs" in r.getMessage()
            for r in caplog.records
        )
        assert not _errors(caplog)

    async def test_error_when_off_and_required(self, caplog) -> None:
        chain = MagicMock()
        chain.get_commit_reveal_enabled = AsyncMock(return_value=False)
        with caplog.at_level(logging.INFO, logger=_LOGGER):
            await _worker(require=True, chain=chain)._log_commit_reveal_mode()
        errs = _errors(caplog)
        assert errs and "front-runnable" in errs[0].getMessage()

    async def test_info_when_off_and_not_required(self, caplog) -> None:
        chain = MagicMock()
        chain.get_commit_reveal_enabled = AsyncMock(return_value=False)
        with caplog.at_level(logging.INFO, logger=_LOGGER):
            await _worker(require=False, chain=chain)._log_commit_reveal_mode()
        assert not _errors(caplog)
        assert any("commit-reveal OFF" in r.getMessage() for r in caplog.records)

    async def test_undeterminable_none_is_fail_open(self, caplog) -> None:
        chain = MagicMock()
        chain.get_commit_reveal_enabled = AsyncMock(return_value=None)
        with caplog.at_level(logging.INFO, logger=_LOGGER):
            # Fail-open: must not raise.
            await _worker(require=True, chain=chain)._log_commit_reveal_mode()
        assert any("undeterminable" in r.getMessage() for r in caplog.records)

    async def test_read_error_is_fail_open(self) -> None:
        chain = MagicMock()
        chain.get_commit_reveal_enabled = AsyncMock(
            side_effect=RuntimeError("pylon down")
        )
        # Must not raise — observability never wedges weight-setting.
        await _worker(require=True, chain=chain)._log_commit_reveal_mode()

    async def test_sink_without_reader_is_noop(self) -> None:
        # A weight sink lacking the reader (e.g. an older sink) is a silent no-op.
        chain = MagicMock(spec=["put_weights"])
        await _worker(require=True, chain=chain)._log_commit_reveal_mode()
