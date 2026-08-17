import pytest

import screener_capacity.oneshot as oneshot_mod


@pytest.fixture(autouse=True)
def _fast_oneshot_delete_settle(monkeypatch) -> None:
    monkeypatch.setattr(oneshot_mod, "DELETED_SETTLE_SECONDS", 0)
