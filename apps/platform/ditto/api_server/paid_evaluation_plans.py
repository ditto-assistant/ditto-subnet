"""Pre-lease approval plans, queue eligibility and safe unused-fund release."""

from __future__ import annotations

import asyncio
import logging
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from fastapi import HTTPException
from sqlalchemy import exists, func, select, update

from ditto.api_server.evaluation_billing import (
    EvaluationBillingConfig,
    _post,
    linked_user,
)
from ditto.db.models import (
    Agent,
    ConfirmationBundle,
    ConfirmationBundleSubject,
    ConfirmationBundleTicket,
    MinerDittoLink,
    PaidEvaluationGrantBinding,
    PaidEvaluationPlan,
    ValidatorTicket,
)

logger = logging.getLogger(__name__)


def required(session) -> bool:
    return bool(session.info.get("paid_evaluation_required", False))


def ready_predicate(
    *,
    bench_version: int,
    now: datetime,
    phase: str = "competition",
    agent_column=None,
    bundle_column=None,
    unused: bool = False,
    minimum_created_at: datetime | None = None,
):
    """Correlated EXISTS filters before LIMIT/slot allocation, avoiding starvation."""
    tests = [
        PaidEvaluationPlan.bench_version == bench_version,
        PaidEvaluationPlan.phase == phase,
        PaidEvaluationPlan.state == "approved",
        PaidEvaluationPlan.expires_at > now,
        PaidEvaluationPlan.refreshed_at > now - timedelta(seconds=90),
    ]
    if unused:
        tests.append(PaidEvaluationPlan.started_at.is_(None))
    if minimum_created_at is not None:
        tests.append(PaidEvaluationPlan.created_at > minimum_created_at)
    if agent_column is not None:
        tests.append(PaidEvaluationPlan.agent_id == agent_column)
    if bundle_column is not None:
        tests.append(PaidEvaluationPlan.bundle_id == bundle_column)
    tests.append(
        exists(
            select(MinerDittoLink.miner_hotkey)
            .join(Agent, Agent.miner_hotkey == MinerDittoLink.miner_hotkey)
            .where(
                Agent.agent_id == PaidEvaluationPlan.agent_id,
                MinerDittoLink.ditto_user_id == PaidEvaluationPlan.payer_uid,
                MinerDittoLink.revoked_at.is_(None),
            )
        )
    )
    return exists(select(PaidEvaluationPlan.run_id).where(*tests))


async def ready_plan(
    session,
    *,
    agent_id: UUID | None,
    bench_version: int,
    phase: str,
    bundle_id: UUID | None = None,
) -> PaidEvaluationPlan | None:
    now = datetime.now(UTC)
    query = select(PaidEvaluationPlan).where(
        PaidEvaluationPlan.bench_version == bench_version,
        PaidEvaluationPlan.phase == phase,
        PaidEvaluationPlan.state == "approved",
        PaidEvaluationPlan.expires_at > now,
        PaidEvaluationPlan.refreshed_at > now - timedelta(seconds=90),
    )
    query = (
        query.where(PaidEvaluationPlan.bundle_id == bundle_id)
        if bundle_id is not None
        else query.where(PaidEvaluationPlan.agent_id == agent_id)
    )
    plan = await session.scalar(query.with_for_update())
    if plan is not None:
        try:
            if await linked_user(session, plan.agent_id) != plan.payer_uid:
                return None
        except HTTPException as error:
            if error.status_code == 402:
                return None
            raise
    return plan


async def bind_grant(session, grant_id: UUID, plan: PaidEvaluationPlan) -> None:
    binding = await session.get(PaidEvaluationGrantBinding, grant_id)
    if binding is None:
        session.add(PaidEvaluationGrantBinding(grant_id=grant_id, run_id=plan.run_id))
    elif binding.run_id != plan.run_id:
        raise HTTPException(
            409, "An earlier evaluation grant cannot spend a new budget"
        )
    plan.started_at = plan.started_at or datetime.now(UTC)


async def plan_for_grant(session, grant_id: UUID) -> PaidEvaluationPlan:
    plan = await session.scalar(
        select(PaidEvaluationPlan)
        .join(
            PaidEvaluationGrantBinding,
            PaidEvaluationGrantBinding.run_id == PaidEvaluationPlan.run_id,
        )
        .where(
            PaidEvaluationGrantBinding.grant_id == grant_id,
            PaidEvaluationPlan.state == "approved",
            PaidEvaluationPlan.expires_at > datetime.now(UTC),
        )
    )
    if plan is None:
        raise HTTPException(402, "This evaluation needs an approved spending budget")
    return plan


async def propose_plan(
    state,
    session,
    *,
    agent: Agent,
    phase: str,
    bench_version: int,
    bundle_id: UUID | None = None,
    explicit: bool = False,
) -> PaidEvaluationPlan:
    """Only trusted auto-selection or an authenticated owning miner calls this.

    An explicit confirmation proposal binds that miner as payer for the WHOLE
    comparison; approval still occurs separately in the payer's Ditto session.
    """
    config: EvaluationBillingConfig = state.config.evaluation_billing
    uid = await linked_user(session, agent.agent_id)
    query = select(PaidEvaluationPlan).where(
        PaidEvaluationPlan.agent_id == agent.agent_id,
        PaidEvaluationPlan.bench_version == bench_version,
        PaidEvaluationPlan.phase == phase,
        PaidEvaluationPlan.state != "closed",
    )
    existing = await session.scalar(query.with_for_update())
    if (
        existing is not None
        and explicit
        and phase == "competition"
        and existing.started_at is not None
    ):
        active = int(
            await session.scalar(
                select(func.count())
                .select_from(ValidatorTicket)
                .where(
                    ValidatorTicket.agent_id == agent.agent_id,
                    ValidatorTicket.bench_version == bench_version,
                    ValidatorTicket.status == "issued",
                    ValidatorTicket.deadline > datetime.now(UTC),
                )
            )
            or 0
        )
        if active:
            raise HTTPException(
                409, "Finish the current evaluation before preparing a fresh run"
            )
        existing.state = "closing"
        await reconcile_plan(state, session, existing)
        if existing.state != "closed":
            raise HTTPException(
                409, "Provider receipts must settle before a fresh evaluation"
            )
        existing = None
    if existing is not None:
        if existing.payer_uid != uid or existing.bundle_id != bundle_id:
            raise HTTPException(
                409,
                "Close the previous budget before changing its payer or purpose",
            )
        return await refresh_plan(state, session, existing)
    if phase == "confirmation":
        bundle = await session.get(ConfirmationBundle, bundle_id)
        subjects = list(
            (
                await session.scalars(
                    select(ConfirmationBundleSubject.agent_id).where(
                        ConfirmationBundleSubject.bundle_id == bundle_id
                    )
                )
            ).all()
        )
        if (
            bundle is None
            or bundle.bench_version != bench_version
            or agent.agent_id not in subjects
        ):
            raise HTTPException(404, "Confirmation is unavailable")
        if bundle.state not in {"pending", "blocked_budget"}:
            raise HTTPException(409, "This confirmation is not waiting for a payer")
        if not explicit and len(subjects) != 1:
            raise HTTPException(
                402, "Confirmation needs an explicit candidate payer binding"
            )
        bound = await session.scalar(
            select(PaidEvaluationPlan)
            .where(
                PaidEvaluationPlan.bundle_id == bundle_id,
                PaidEvaluationPlan.state != "closed",
            )
            .with_for_update()
        )
        if bound is not None:
            raise HTTPException(409, "This confirmation already has a payer")
    cycle = (
        int(
            await session.scalar(
                select(func.max(PaidEvaluationPlan.cycle)).where(
                    PaidEvaluationPlan.agent_id == agent.agent_id,
                    PaidEvaluationPlan.bench_version == bench_version,
                    PaidEvaluationPlan.phase == phase,
                )
            )
            or 0
        )
        + 1
    )
    estimate = config.estimate_microusd
    # Full frozen confirmation includes reader, judge and embedding maxima.
    # Its payer is shown the whole comparison, not just its own answer column.
    if phase == "confirmation":
        profile = getattr(state, "confirmation_verification_profiles", {}).get(
            (bundle.profile_revision, bundle.profile_checksum)
        )
        if profile is None:
            raise HTTPException(503, "The frozen confirmation profile is unavailable")
        estimate = max(
            estimate,
            sum(lane.max_cost_usd_micros for lane in profile.provider_lanes)
            + profile.embedding_lane.max_cost_usd_micros,
        )
    cap = max(config.cap_microusd, estimate)
    if cap > 50_000_000:
        raise HTTPException(
            402, "The full evaluation estimate exceeds the configured billing limit"
        )
    now = datetime.now(UTC)
    plan = PaidEvaluationPlan(
        run_id=uuid4(),
        agent_id=agent.agent_id,
        bench_version=bench_version,
        phase=phase,
        cycle=cycle,
        bundle_id=bundle_id,
        payer_uid=uid,
        state="proposed",
        estimate_microusd=estimate,
        cap_microusd=cap,
        expires_at=now + timedelta(hours=2),
        refreshed_at=now,
    )
    session.add(plan)
    await session.flush()
    return await refresh_plan(state, session, plan)


async def refresh_plan(state, _session, plan: PaidEvaluationPlan) -> PaidEvaluationPlan:
    config = state.config.evaluation_billing
    now = datetime.now(UTC)
    run = await _post(
        state.inference_client,
        config,
        plan.payer_uid,
        "/runs",
        {
            "runId": str(plan.run_id),
            "agentId": str(plan.agent_id),
            "estimateMicrousd": plan.estimate_microusd,
            "capMicrousd": plan.cap_microusd,
            "phase": plan.phase,
        },
    )
    if run.get("state") not in {"proposed", "approved", "closed", "canceled"}:
        raise HTTPException(503, "Evaluation budget status is unavailable")
    plan.expires_at = datetime.fromisoformat(run["expiresAt"].replace("Z", "+00:00"))
    plan.refreshed_at = now
    plan.state = {
        "proposed": "proposed",
        "approved": "approved",
        "closed": "closed",
        "canceled": "closed",
    }[run["state"]]
    if plan.expires_at <= now and plan.state != "closed":
        plan.state = "closing"
    return plan


async def reconcile_plan(state, session, plan: PaidEvaluationPlan) -> None:
    now = datetime.now(UTC)
    terminal = plan.expires_at <= now or plan.state == "closing"
    if plan.started_at is not None and not terminal:
        if plan.phase == "confirmation":
            bundle = await session.get(ConfirmationBundle, plan.bundle_id)
            terminal = bundle is None or bundle.state in {
                "completed",
                "failed",
                "superseded",
                "canceled",
            }
        else:
            agent = await session.get(Agent, plan.agent_id)
            active = int(
                await session.scalar(
                    select(func.count())
                    .select_from(ValidatorTicket)
                    .where(
                        ValidatorTicket.agent_id == plan.agent_id,
                        ValidatorTicket.bench_version == plan.bench_version,
                        ValidatorTicket.status == "issued",
                        ValidatorTicket.deadline > now,
                    )
                )
                or 0
            )
            terminal = active == 0 and (
                agent is None
                or str(agent.status)
                in {
                    "scored",
                    "live",
                    "ath_pending_review",
                    "rejected",
                    "banned",
                    "screening_failed",
                }
            )
    if terminal:
        plan.state = "closing"
        if plan.expires_at <= now and plan.phase == "competition":
            # No new provider work may follow an expired approval. Existing
            # in-flight receipts still reconcile against their old held run.
            await session.execute(
                update(ValidatorTicket)
                .where(
                    ValidatorTicket.agent_id == plan.agent_id,
                    ValidatorTicket.bench_version == plan.bench_version,
                    ValidatorTicket.status == "issued",
                )
                .values(status="expired", deadline=now)
            )
        if plan.expires_at <= now and plan.phase == "confirmation":
            await session.execute(
                update(ConfirmationBundleTicket)
                .where(
                    ConfirmationBundleTicket.bundle_id == plan.bundle_id,
                    ConfirmationBundleTicket.status == "issued",
                )
                .values(status="expired", deadline=now)
            )
        try:
            await _post(
                state.inference_client,
                state.config.evaluation_billing,
                plan.payer_uid,
                "/runs/" + str(plan.run_id) + "/close",
                {},
            )
        except HTTPException as error:
            if error.status_code == 409:
                return  # Unknown/in-flight cost stays held, never guessed away.
            raise
        plan.state = "closed"
        plan.refreshed_at = now
    else:
        await refresh_plan(state, session, plan)


class EvaluationPlanLoop:
    def __init__(self, state):
        self.state = state
        self.task = None

    async def start(self):
        if self.state.config.evaluation_billing.enabled:
            self.task = asyncio.create_task(
                self.run(), name="evaluation-budget-reconciler"
            )

    async def aclose(self):
        if self.task:
            self.task.cancel()
            with suppress(asyncio.CancelledError):
                await self.task

    async def tick(self):
        # Unapproved plans never allocate a validator/scorer slot. Initial
        # linked submissions receive a visible proposal before queue admission.
        async with self.state.session_maker() as session:
            agents = list(
                (
                    await session.scalars(
                        select(Agent)
                        .where(
                            Agent.status.in_(["evaluating", "screening_passed"]),
                            ~exists(
                                select(PaidEvaluationPlan.run_id).where(
                                    PaidEvaluationPlan.agent_id == Agent.agent_id,
                                    PaidEvaluationPlan.phase == "competition",
                                    PaidEvaluationPlan.state != "closed",
                                )
                            ),
                        )
                        .order_by(Agent.created_at)
                        .limit(100)
                    )
                ).all()
            )
            ids = [a.agent_id for a in agents]
        for agent_id in ids:
            try:
                async with self.state.session_maker() as session, session.begin():
                    agent = await session.scalar(
                        select(Agent)
                        .where(Agent.agent_id == agent_id)
                        .with_for_update()
                    )
                    from ditto.db.queries.benchmark_rollout import active_bench_version

                    bench = await active_bench_version(session)
                    await propose_plan(
                        self.state,
                        session,
                        agent=agent,
                        phase="competition",
                        bench_version=bench,
                    )
            except HTTPException:
                continue
        async with self.state.session_maker() as session:
            bundles = list(
                (
                    await session.scalars(
                        select(ConfirmationBundle)
                        .where(
                            ConfirmationBundle.state.in_(["pending", "blocked_budget"]),
                            ~exists(
                                select(PaidEvaluationPlan.run_id).where(
                                    PaidEvaluationPlan.bundle_id
                                    == ConfirmationBundle.bundle_id,
                                    PaidEvaluationPlan.state != "closed",
                                )
                            ),
                            select(func.count())
                            .select_from(ConfirmationBundleSubject)
                            .where(
                                ConfirmationBundleSubject.bundle_id
                                == ConfirmationBundle.bundle_id
                            )
                            .scalar_subquery()
                            == 1,
                        )
                        .order_by(ConfirmationBundle.created_at)
                        .limit(100)
                    )
                ).all()
            )
            bundle_ids = [b.bundle_id for b in bundles]
        for bundle_id in bundle_ids:
            try:
                async with self.state.session_maker() as session, session.begin():
                    subjects = list(
                        (
                            await session.scalars(
                                select(ConfirmationBundleSubject.agent_id)
                                .where(ConfirmationBundleSubject.bundle_id == bundle_id)
                                .limit(2)
                            )
                        ).all()
                    )
                    if len(subjects) != 1:
                        continue  # Visible explicit-payer binding is required.
                    agent = await session.scalar(
                        select(Agent)
                        .where(Agent.agent_id == subjects[0])
                        .with_for_update()
                    )
                    bundle = await session.get(ConfirmationBundle, bundle_id)
                    await propose_plan(
                        self.state,
                        session,
                        agent=agent,
                        phase="confirmation",
                        bench_version=bundle.bench_version,
                        bundle_id=bundle_id,
                    )
            except HTTPException:
                continue
        async with self.state.session_maker() as session:
            ids = list(
                (
                    await session.scalars(
                        select(PaidEvaluationPlan.run_id)
                        .where(PaidEvaluationPlan.state != "closed")
                        .order_by(
                            PaidEvaluationPlan.refreshed_at,
                            PaidEvaluationPlan.created_at,
                        )
                        .limit(100)
                    )
                ).all()
            )
        for run_id in ids:
            try:
                async with self.state.session_maker() as session, session.begin():
                    snapshot = await session.get(PaidEvaluationPlan, run_id)
                    if snapshot is None:
                        continue
                    if snapshot.bundle_id is not None:
                        await session.get(
                            ConfirmationBundle, snapshot.bundle_id, with_for_update=True
                        )
                    await session.get(Agent, snapshot.agent_id, with_for_update=True)
                    plan = await session.get(
                        PaidEvaluationPlan,
                        run_id,
                        with_for_update=True,
                        populate_existing=True,
                    )
                    await reconcile_plan(self.state, session, plan)
            except HTTPException:
                continue

    async def run(self):
        while True:
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("evaluation budget reconciliation failed")
            await asyncio.sleep(30)
