"""Per-mechanism aggregate + per-case report writer.

Produces JSON output matching ``schemas/score.schema.json`` for individual
case records, plus a top-level aggregate that captures the mean score per
mechanism, per-category breakdown means, and the count of cases scored.
"""

from __future__ import annotations

import json
import statistics
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ditto.bench import SCHEMA_VERSION
from ditto.bench.loader.taxonomy import Mechanism
from ditto.bench.runner.scoring import Score


@dataclass(slots=True)
class MechanismAggregate:
    """Per-mechanism summary across all scored cases."""

    mechanism: Mechanism
    count: int
    mean_score: float
    per_category: dict[str, float] = field(default_factory=dict)
    per_component_mean: dict[str, float] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """Serialise to a JSON-ready dict."""
        return {
            "mechanism": str(self.mechanism),
            "count": self.count,
            "mean_score": self.mean_score,
            "per_category": self.per_category,
            "per_component_mean": self.per_component_mean,
        }


@dataclass(slots=True)
class Report:
    """Top-level report: aggregates plus the full per-case score list."""

    schema_version: str
    generated_at: datetime
    image: str
    aggregates: list[MechanismAggregate] = field(default_factory=list)
    scores: list[Score] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        """Serialise to a JSON-ready dict."""
        return {
            "schema_version": self.schema_version,
            "generated_at": self.generated_at.isoformat().replace("+00:00", "Z"),
            "image": self.image,
            "aggregates": [a.to_dict() for a in self.aggregates],
            "scores": [s.to_dict() for s in self.scores],
        }


def aggregate(scores: list[Score]) -> list[MechanismAggregate]:
    """Group ``scores`` by mechanism and compute summary statistics.

    Returns one :class:`MechanismAggregate` per mechanism that appears in
    the input, in stable mechanism-string order so the report is
    deterministic across runs.
    """
    by_mech: dict[Mechanism, list[Score]] = {}
    for s in scores:
        by_mech.setdefault(s.mechanism, []).append(s)

    aggregates: list[MechanismAggregate] = []
    for mech in sorted(by_mech.keys(), key=str):
        group = by_mech[mech]
        per_category: dict[str, list[float]] = {}
        per_component: dict[str, list[float]] = {}
        for s in group:
            if s.category:
                per_category.setdefault(s.category, []).append(s.score)
            for k, v in s.breakdown.items():
                per_component.setdefault(k, []).append(v)

        aggregates.append(
            MechanismAggregate(
                mechanism=mech,
                count=len(group),
                mean_score=statistics.fmean(s.score for s in group),
                per_category={
                    k: statistics.fmean(v) for k, v in sorted(per_category.items())
                },
                per_component_mean={
                    k: statistics.fmean(v) for k, v in sorted(per_component.items())
                },
            )
        )
    return aggregates


def write_report(
    scores: list[Score],
    image: str,
    out_path: Path | str,
) -> Report:
    """Build and write a full :class:`Report` to ``out_path`` as JSON."""
    report = Report(
        schema_version=SCHEMA_VERSION,
        generated_at=datetime.now(UTC),
        image=image,
        aggregates=aggregate(scores),
        scores=list(scores),
    )
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report.to_dict(), indent=2), encoding="utf-8")
    return report
