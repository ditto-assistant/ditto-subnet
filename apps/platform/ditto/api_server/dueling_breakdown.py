"""Public read-side projection of a bench v16+ score's dueling arithmetic.

``scores.dueling`` holds the signed report's ``details.dueling`` block exactly
as ingestion validated it (:class:`DuelingReport`), and ``scores.gate_evidence``
holds the run's ``priced_cost`` summary. This module turns both into
:class:`PublicDuelingBreakdown` for the public score reads, Backroom's
``get_agent_scores`` and the dashboard.

Disclosure mirrors the Backroom canary read (``benchmark-canary.schemas.ts``):
the datagen half, gate, column weight and each column's ``n``/``sum`` and
substitution flag. The owner key, generator submission ids, ``sum_sq`` and the
slice digest never leave this module; a column's ownership is reduced to
whether it was self-masked for this agent. Nothing here feeds ranking, weights
or ingestion, and a malformed cell reads as "no breakdown", never an error.
"""

from __future__ import annotations

import logging
import math

from pydantic import ValidationError

from ditto.api_models.public import PublicDuelingBreakdown, PublicDuelingColumn
from ditto.api_server.gate_evidence import stored_gate_evidence
from ditto_screening_protocol.dueling import (
    DuelingReport,
    is_dueling_bench_version,
)

logger = logging.getLogger(__name__)


def _unit(value: float) -> float:
    """Clamp sub-ulp float noise into [0, 1]; the inputs are already bounded."""
    return min(1.0, max(0.0, value))


def public_dueling_breakdown(
    *, bench_version: int | None, dueling: object, gate_evidence: object
) -> PublicDuelingBreakdown | None:
    """The public dueling breakdown of one stored v16+ score, or ``None``."""
    if bench_version is None or not is_dueling_bench_version(bench_version):
        return None
    if not isinstance(dueling, dict):
        return None
    try:
        report = DuelingReport.model_validate(dueling)
    except ValidationError:
        logger.warning("ignoring malformed stored scores.dueling")
        return None

    weight = report.column_weight
    owner = report.owner_key
    columns: list[PublicDuelingColumn] = []
    counted = 0
    generator = 0.0
    # Column-id order, every product rounded before it is added: the same
    # float64 sequence as ``dueling_composite`` so the recomputation is exact.
    for column in sorted(report.columns, key=lambda c: c.column_id):
        masked = column.owner_key != "" and column.owner_key == owner
        mean = column.mean()
        contribution = 0.0 if masked else weight * mean
        if not masked:
            counted += 1
            generator = generator + contribution
        columns.append(
            PublicDuelingColumn(
                column_id=column.column_id,
                grading_bench_version=column.grading_bench_version,
                n=column.n,
                sum=column.sum,
                mean=_unit(mean),
                substituted=column.substituted,
                public_default=column.owner_key == "" and not column.substituted,
                self_masked=masked,
                contribution=_unit(contribution),
            )
        )
    share = 1 - weight * float(counted)
    inner = share * report.datagen_raw + generator

    priced = None
    stored = stored_gate_evidence(gate_evidence)
    if stored is not None:
        priced = stored.priced_cost
    applied_bps = priced.applied_factor_bps if priced is not None else None
    factor = applied_bps / 10_000 if applied_bps is not None else None
    other = (
        _unit(report.datagen_gate / factor)
        if factor is not None and factor > 0 and math.isfinite(report.datagen_gate)
        else None
    )
    try:
        return PublicDuelingBreakdown(
            season_id=report.season_id,
            season_index=report.season_index,
            datagen_raw=report.datagen_raw,
            datagen_gate=report.datagen_gate,
            column_weight=weight,
            counted_columns=counted,
            datagen_share=_unit(share),
            generator_sum=_unit(generator),
            pre_gate_composite=_unit(inner),
            composite=_unit(report.self_composite()),
            composite_stderr=report.composite_stderr,
            priced_cost_applied_factor_bps=applied_bps,
            priced_cost_factor_bps=priced.factor_bps if priced is not None else None,
            priced_cost_factor=factor,
            priced_cost_posture=priced.posture if priced is not None else None,
            priced_cost_result=priced.result if priced is not None else None,
            priced_cost_window=priced.window if priced is not None else None,
            other_gate_factor=other,
            columns=columns,
        )
    except ValidationError:
        logger.warning("dropping an unrepresentable dueling breakdown")
        return None
