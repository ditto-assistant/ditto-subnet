"""Admin: explain why a submission is (or isn't) leaseable for scoring.

Reports the prerequisite checks in ``issue_ticket`` (dataset + screened image +
screening policy + evaluating status) so an operator can see, without DB access,
why an agent sits below quorum with no validator ever picking it up — the
classic "0/3 on v4 but never leased" case, usually a missing v4 dataset or an
unbuilt screened image.

This is a **read model, not a gate**: nothing in the lease path consults it, so
it can only ever be an approximation of ``issue_ticket``. Report it as evidence,
never as authority. Two known gaps it does not model: the bounded per-validator
attempt budget, and per-validator/per-slot occupancy — an agent this endpoint
calls leaseable may still wait because no compatible validator slot is free.

Crucially, the prerequisites are keyed by benchmark version, and during an open
rollout "the" version is ambiguous. A submission received after the rollout
opened is queued in the *desired* era (its dataset is rendered there by
``screener.submit_result``, and validators lease it there through the
fresh-submission lane in ``request_job``), while older submissions stay on the
active version unless they are rollout cohort members. Answering against the
active version alone reports a missing source-version dataset for a submission
that is queued, admitted, and leaseable in the desired era — a false negative
that has already cost an operator investigation. ``scoring_bench_version`` below
names the era each answer is about.
"""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import exists, select
from sqlalchemy.ext.asyncio import AsyncSession

from ditto.api_models.admin_scoring_readiness import (
    AgentScoringReadiness,
    ScreenedImageReadiness,
)
from ditto.api_models.agent_status import AgentStatus
from ditto.api_models.benchmark_contract import benchmark_contract
from ditto.api_server.dependencies import get_session
from ditto.api_server.endpoints.admin_quarantine import require_admin
from ditto.db.models import Agent, BenchmarkDataset
from ditto.db.queries.benchmark_admission import agent_is_admitted
from ditto.db.queries.benchmark_rollout import (
    active_bench_version,
    agent_is_rollout_member,
    arrival_bench_version,
    open_rollout,
)
from ditto.screener_policy_state import effective_screening_policy_version

router = APIRouter(prefix="/admin", tags=["admin"])
SessionDep = Annotated[AsyncSession, Depends(get_session)]
AdminDep = Annotated[None, Depends(require_admin)]

# The screened-image columns issue_ticket requires all-present before a
# screened-only benchmark will lease the agent (see tickets.py
# ``complete_screened_image``).
_SCREENED_IMAGE_FIELDS = (
    "screened_image_sha256",
    "screened_image_size_bytes",
    "screened_image_id",
    "screened_image_ref",
    "screened_image_upload_id",
    "screened_image_verified_at",
)


@router.get(
    "/agents/{agent_id}/scoring-readiness", response_model=AgentScoringReadiness
)
async def scoring_readiness(
    agent_id: UUID, _admin: AdminDep, session: SessionDep
) -> AgentScoringReadiness:
    agent = await session.scalar(select(Agent).where(Agent.agent_id == agent_id))
    if agent is None:
        raise HTTPException(status_code=404, detail="agent not found")

    active_version = await active_bench_version(session)
    # The era this submission is actually queued in. Arrival into an open
    # rollout's desired era is one lane; being a frozen cohort member rescored
    # out of ``issue_rollout_ticket`` is the other. Both are leased at the
    # desired version, so answering against the active version alone reports a
    # missing source-version dataset for work that is queued and leaseable.
    rollout = await open_rollout(session)
    cohort_member = rollout is not None and await agent_is_rollout_member(
        session, rollout=rollout, agent_id=agent_id
    )
    scoring_version = await arrival_bench_version(session, agent=agent)
    if rollout is not None and cohort_member:
        scoring_version = rollout.desired_version
    contract = benchmark_contract(scoring_version)

    missing_fields = [
        field for field in _SCREENED_IMAGE_FIELDS if getattr(agent, field) is None
    ]
    image_complete = not missing_fields
    image_verified = agent.screened_image_verified_at is not None
    policy_ok = (
        agent.screening_policy_version >= contract.minimum_screening_policy_version
    )
    image_eligible = image_complete and policy_ok

    # A versioned dataset is only a prerequisite off the legacy v2 path (see the
    # ``bench_version != 2`` guard in issue_ticket).
    has_versioned_dataset = bool(
        await session.scalar(
            select(
                exists().where(
                    BenchmarkDataset.agent_id == agent_id,
                    BenchmarkDataset.bench_version == scoring_version,
                )
            )
        )
    )
    benchmark_admitted = await agent_is_admitted(
        session, bench_version=scoring_version, agent_id=agent_id
    )

    blocking: list[str] = []
    # The cohort lane rescores frozen members straight out of ``scored``/``live``
    # (see ``issue_rollout_ticket``); only the ordinary and fresh-submission
    # lanes require ``evaluating``.
    leaseable_statuses = (
        (AgentStatus.SCORED, AgentStatus.LIVE)
        if cohort_member
        else (AgentStatus.EVALUATING,)
    )
    if agent.status not in leaseable_statuses:
        expected = " or ".join(status.value for status in leaseable_statuses)
        lane = "rollout cohort" if cohort_member else "scoring"
        blocking.append(
            f"agent status is '{agent.status.value}', not {expected} "
            f"(the {lane} lane leases no other status)"
        )
    required_version = effective_screening_policy_version()
    if agent.screening_policy_version < required_version:
        blocking.append(
            f"screening policy v{agent.screening_policy_version} is below the "
            f"required v{required_version} — needs a re-screen"
        )
    if contract.requires_screened_image and not image_eligible:
        if not image_complete:
            blocking.append(
                "screened image is not built yet (missing: "
                + ", ".join(missing_fields)
                + ") — needs a build-only re-screen"
            )
        else:
            blocking.append(
                f"screened image was built under a policy below the v{scoring_version} "
                f"contract minimum v{contract.minimum_screening_policy_version}"
            )
    if scoring_version != 2 and not has_versioned_dataset:
        blocking.append(
            f"no v{scoring_version} benchmark dataset has been generated "
            "for this agent yet"
        )
    if not benchmark_admitted:
        blocking.append(
            f"historical submission is not admitted to the v{scoring_version} "
            "validator queue"
        )

    return AgentScoringReadiness(
        agent_id=agent.agent_id,
        agent_name=agent.name,
        miner_hotkey=agent.miner_hotkey,
        status=agent.status.value,
        active_bench_version=active_version,
        scoring_bench_version=scoring_version,
        scoring_lane=(
            "rollout_cohort"
            if cohort_member
            else (
                "fresh_submission" if scoring_version != active_version else "ordinary"
            )
        ),
        screening_policy_version=agent.screening_policy_version,
        required_screening_policy_version=effective_screening_policy_version(),
        requires_screened_image=contract.requires_screened_image,
        has_versioned_dataset=has_versioned_dataset,
        screened_image=ScreenedImageReadiness(
            complete=image_complete,
            verified=image_verified,
            policy_ok=policy_ok,
            missing_fields=missing_fields,
        ),
        leaseable=not blocking,
        blocking_reasons=blocking,
    )
