"""Bounded catalog races and sentinel-only recovery, no database required."""

from unittest.mock import MagicMock

import pytest

from ditto.db import audit_index_recovery as recovery


@pytest.mark.parametrize("state", [None, False, True])
def test_sentinel_recovery_accepts_missing_or_invalid_but_not_valid(monkeypatch, state):
    connection = MagicMock()
    connection.get_isolation_level.return_value = "READ COMMITTED"
    run = MagicMock(side_effect=[RuntimeError(recovery.FAILURE), None])
    repair = MagicMock()
    monkeypatch.setattr(recovery, "_index_state", lambda _: state)
    monkeypatch.setattr(recovery, "_ensure_valid_index", repair)
    if state is True:
        with pytest.raises(RuntimeError, match="did not come up valid"):
            recovery.run_with_audit_index_recovery(connection, run)
        repair.assert_not_called()
        assert run.call_count == 1
    else:
        recovery.run_with_audit_index_recovery(connection, run)
        repair.assert_called_once_with(connection)
        assert run.call_count == 2
        assert connection.execution_options.call_args.kwargs == {
            "isolation_level": "READ COMMITTED"
        }


def test_unrelated_failure_never_enters_recovery(monkeypatch):
    state = MagicMock()
    monkeypatch.setattr(recovery, "_index_state", state)
    connection = MagicMock()
    with pytest.raises(RuntimeError, match="unrelated failure"):
        recovery.run_with_audit_index_recovery(
            connection, MagicMock(side_effect=RuntimeError("unrelated failure"))
        )
    state.assert_not_called()
    connection.execution_options.assert_not_called()


def test_invalid_index_created_between_catalog_read_and_create_is_rebuilt(monkeypatch):
    connection = MagicMock()
    # Absent at first read; another builder's INVALID object makes CREATE a
    # no-op. The next attempt must inspect, drop, create and validate again.
    monkeypatch.setattr(
        recovery, "_index_state", MagicMock(side_effect=[None, False, False, True])
    )
    monkeypatch.setattr(recovery, "backoff_delay", lambda _: 0)
    recovery._ensure_valid_index(connection)
    statements = [call.args[0] for call in connection.exec_driver_sql.call_args_list]
    assert len(statements) == 3
    assert statements[0].startswith("CREATE INDEX CONCURRENTLY")
    assert statements[1].startswith("DROP INDEX CONCURRENTLY")
    assert statements[2].startswith("CREATE INDEX CONCURRENTLY")


def test_invalid_postcondition_exhausts_bounded_attempts(monkeypatch):
    connection = MagicMock()
    monkeypatch.setattr(recovery, "MAX_ATTEMPTS", 2)
    monkeypatch.setattr(recovery, "_index_state", lambda _: False)
    monkeypatch.setattr(recovery, "backoff_delay", lambda _: 0)
    with pytest.raises(RuntimeError, match="did not come up valid"):
        recovery._ensure_valid_index(connection)
    assert connection.exec_driver_sql.call_count == 4
