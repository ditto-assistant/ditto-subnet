from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from ditto.db.queries.inference_observability import load_inference_runtime_rows


@pytest.mark.asyncio
async def test_empty_runtime_metrics_read_is_total(session: AsyncSession) -> None:
    current, windows, peaks = await load_inference_runtime_rows(
        session,
        stale_after_seconds=60,
    )

    assert [row["request_kind"] for row in current] == ["chat", "embedding"]
    assert all(row["active_requests"] == 0 for row in current)
    assert all(row["live_grants"] == 0 for row in current)
    assert windows == []
    assert peaks == []
