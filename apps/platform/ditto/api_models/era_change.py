"""Why a submission reads ``era_changed``, and what the miner can do next.

A benchmark rollout is forward-only. Once a version activates, validators score
only that version, and the queue admits only what
:func:`~ditto.db.queries.benchmark_admission.benchmark_admission_predicate`
names. Earlier-era work that predicate refuses can never be scored or ranked
on the active version. Without a status that says so, it read "evaluating"
(still queued) or "scored" (finalized on the previous version) forever
(SN-462, SN-483, SN-485).

``era_changed`` is that terminal miner-facing status. It is derived at read time
from the queue's own predicate. It changes no queue, lease, admission, retry or
score state, and it clears by itself when an operator or a rollout admits the
row.
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

EraChangeReason = Literal["not_admitted_to_active_bench"]
EraChangeNextStep = Literal["resubmit"]


class PublicEraChange(BaseModel):
    """Set only on an ``era_changed`` row."""

    model_config = ConfigDict(extra="ignore")

    reason: Annotated[
        EraChangeReason,
        Field(
            description=(
                "'not_admitted_to_active_bench': the submission belongs to an "
                "earlier bench version, and nothing admits it to the active "
                "one (not the rollout cohort, a previous-generation carryover, "
                "an operator admission or an audited contract refresh). "
                "Validators score only the active version, so it is not scored "
                "or ranked there. This is not a rejection."
            )
        ),
    ]
    bench_version: Annotated[
        int,
        Field(ge=1, description="The active bench version it does not compete on."),
    ]
    scored_bench_version: Annotated[
        int | None,
        Field(
            default=None,
            ge=1,
            description=(
                "The earlier bench version this submission finalized on, when "
                "it was scored there. Its scores and history on that version "
                "stay visible. Null for a submission that was still waiting "
                "for scores when the era changed."
            ),
        ),
    ] = None
    next_step: Annotated[
        EraChangeNextStep,
        Field(
            description=(
                "'resubmit': upload a new version to compete on the active "
                "bench version."
            )
        ),
    ]


__all__ = ["EraChangeNextStep", "EraChangeReason", "PublicEraChange"]
