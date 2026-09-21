"""The dashboard keys its "no finding" treatment on Platform's public reason."""

from __future__ import annotations

import re
from pathlib import Path

from ditto.api_server.deferred_source_review import (
    SOURCE_REVIEW_INCONCLUSIVE_PUBLIC_REASON,
)

_STATUS_TS = (
    Path(__file__).resolve().parents[3]
    / "dashboard"
    / "src"
    / "components"
    / "pipeline"
    / "status.ts"
)


def test_dashboard_inconclusive_reason_matches_platform_constant() -> None:
    source = _STATUS_TS.read_text(encoding="utf-8")
    match = re.search(
        r"SOURCE_REVIEW_INCONCLUSIVE_REASON\s*=\s*\n?\s*\"([^\"]*)\"", source
    )
    assert match is not None, (
        "SOURCE_REVIEW_INCONCLUSIVE_REASON string literal not found in "
        f"{_STATUS_TS}; update this test if the dashboard constant moved"
    )
    assert match.group(1) == SOURCE_REVIEW_INCONCLUSIVE_PUBLIC_REASON, (
        "dashboard SOURCE_REVIEW_INCONCLUSIVE_REASON (status.ts) and Platform "
        "SOURCE_REVIEW_INCONCLUSIVE_PUBLIC_REASON (deferred_source_review.py) "
        "differ; change both in the same commit so the dashboard keeps "
        "recognising budget-exhausted holds"
    )
