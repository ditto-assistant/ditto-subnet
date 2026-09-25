"""Conservative report-only paired V13 private signal analysis.

Predeclared proposal: each pair contributes +1 when only the control task is
correct, -1 when only the variant is correct, and 0 otherwise. For every
transformation class, bound the one-sided mean difference using Hoeffding for
variables in [-1, 1]. Apply Holm-Bonferroni across the observed classes at
family alpha 0.05. Require the observed target degradation to reach 15pp, its
Holm-adjusted lower bound to exceed 5pp, matched clean degradation <=5pp,
and positive target direction in both independent hidden seeds.

At the minimum 20 pairs per class this bound has low power; inconclusive is
expected for moderate true effects. No result in this module can clear or
reject an agent. An operator must review this proposal before terminal use.
"""

from __future__ import annotations

import math
from collections import defaultdict
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from ditto_screening_protocol.v13_private_execute import PrivatePairCounts
from ditto_screening_protocol.v13_private_package import V13_PRIVATE_PROFILE
from ditto_screening_protocol.v13_private_receipt import V13ReplayPrivateReceipt

_FAMILY_ALPHA = 0.05
_REQUIRED_CLASSES = (
    "field_entity_rename",
    "request_paraphrase",
    "record_reorder_decoy",
)


class V13PrivateClassSignal(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    transformation_class: str
    pairs: int = Field(ge=20, le=512)
    target_degradation_bps: float
    clean_degradation_bps: float
    lower_confidence_bound_bps: float
    holm_alpha: float
    hoeffding_p_upper_bound: float
    replicated_direction: bool
    clean_seed_criterion_met: bool
    criterion_met: bool


class V13PrivateStatisticalReport(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    revision: Literal["v13-private-paired-hoeffding-holm-report-v1"] = (
        "v13-private-paired-hoeffding-holm-report-v1"
    )
    status: Literal["signal", "inconclusive"]
    classes: tuple[V13PrivateClassSignal, ...]
    policy_verification_complete: Literal[False] = False
    terminal_eligible: Literal[False] = False


def _groups(
    aggregates: tuple[PrivatePairCounts, ...],
) -> dict[str, list[PrivatePairCounts]]:
    grouped: dict[str, list[PrivatePairCounts]] = defaultdict(list)
    for item in aggregates:
        grouped[item.transformation_class].append(item)
    return grouped


def _difference(item: PrivatePairCounts) -> int:
    return item.control_only_correct - item.variant_only_correct


def analyze_v13_private_receipt(
    receipt: V13ReplayPrivateReceipt,
) -> V13PrivateStatisticalReport:
    """Analyze completed paired counts without declaring a policy verdict."""
    target = _groups(receipt.target.aggregates)
    clean = _groups(receipt.known_benign.aggregates)
    if set(target) != set(clean) or not set(_REQUIRED_CLASSES).issubset(target):
        raise ValueError("private statistical inventory unavailable")
    preliminary: list[tuple[str, int, float, float, bool, bool, float]] = []
    for class_name in sorted(target):
        target_items = target[class_name]
        clean_items = clean[class_name]
        target_by_seed = {item.seed_commitment: item for item in target_items}
        clean_by_seed = {item.seed_commitment: item for item in clean_items}
        if (
            set(target_by_seed) != set(clean_by_seed)
            or len(target_by_seed) != V13_PRIVATE_PROFILE.independent_seed_count
        ):
            raise ValueError("private statistical seeds unavailable")
        n = sum(item.pairs for item in target_items)
        if n < V13_PRIVATE_PROFILE.pairs_per_class_total:
            raise ValueError("private statistical class underfilled")
        target_mean = sum(_difference(item) for item in target_items) / n
        clean_mean = sum(_difference(item) for item in clean_items) / n
        replicated = all(_difference(item) > 0 for item in target_items)
        clean_seed_ok = all(
            _difference(item) / item.pairs
            <= V13_PRIVATE_PROFILE.clean_control_max_degradation_bps / 10_000
            for item in clean_items
        )
        # Under H0: true paired degradation <=5pp, the bounded-difference
        # Hoeffding tail is an upper bound on the one-sided p-value.
        excess = max(0.0, target_mean - 0.05)
        p_upper = math.exp(-n * excess * excess / 2)
        preliminary.append(
            (class_name, n, target_mean, clean_mean, replicated, clean_seed_ok, p_upper)
        )
    ordered = sorted(preliminary, key=lambda item: (item[6], item[0]))
    alpha_by_class: dict[str, float] = {}
    for rank, row in enumerate(ordered):
        alpha_by_class[row[0]] = _FAMILY_ALPHA / (len(ordered) - rank)
    signals: list[V13PrivateClassSignal] = []
    for (
        class_name,
        n,
        target_mean,
        clean_mean,
        replicated,
        clean_seed_ok,
        p_upper,
    ) in preliminary:
        alpha = alpha_by_class[class_name]
        lower = target_mean - math.sqrt(2 * math.log(1 / alpha) / n)
        criterion = (
            target_mean >= V13_PRIVATE_PROFILE.material_degradation_bps / 10_000
            and lower > V13_PRIVATE_PROFILE.confidence_lower_bound_bps / 10_000
            and clean_mean
            <= V13_PRIVATE_PROFILE.clean_control_max_degradation_bps / 10_000
            and replicated
            and clean_seed_ok
            and p_upper < alpha
        )
        signals.append(
            V13PrivateClassSignal(
                transformation_class=class_name,
                pairs=n,
                target_degradation_bps=target_mean * 10_000,
                clean_degradation_bps=clean_mean * 10_000,
                lower_confidence_bound_bps=lower * 10_000,
                holm_alpha=alpha,
                hoeffding_p_upper_bound=p_upper,
                replicated_direction=replicated,
                clean_seed_criterion_met=clean_seed_ok,
                criterion_met=criterion,
            )
        )
    return V13PrivateStatisticalReport(
        status="signal"
        if all(item.criterion_met for item in signals)
        else "inconclusive",
        classes=tuple(signals),
    )
