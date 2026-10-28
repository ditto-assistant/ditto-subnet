"""Real-Postgres plan/admission tests; payment HTTP is an isolated service seam."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from fastapi import HTTPException
from sqlalchemy import func, select

from ditto.api_server.evaluation_billing import (
    EvaluationBillingConfig,
    PaidEvaluationCall,
    begin_paid_evaluation,
)
from ditto.api_server.paid_evaluation_plans import (
    bind_grant,
    plan_for_grant,
    propose_plan,
    reconcile_plan,
    refresh_plan,
)
from ditto.db.models import Agent, ValidatorTicket
from ditto.db.queries.miner_ditto_links import upsert_link
from ditto.db.queries.tickets import issue_ticket
from ditto.tests.db.queries.test_tickets import _seed_evaluating


@pytest.mark.asyncio
async def test_maintenance_cannot_reuse_started_cap_and_explicit_fresh_cycle_resumes(
    session_maker,
):
    from ditto.db.queries.tickets import issue_confirmation_ticket

    http = BillingHTTP()
    async with (
        httpx.AsyncClient(transport=httpx.MockTransport(http.handle)) as client,
        session_maker() as session,
    ):
        state = SimpleNamespace(
            config=SimpleNamespace(
                evaluation_billing=EvaluationBillingConfig(
                    enabled=True, api_key="isolated-test"
                )
            ),
            inference_client=client,
        )
        aid = await _seed_evaluating(session, name="fresh-maintenance")
        async with session.begin():
            agent = await linked(session, aid)
            old = await propose_plan(
                state, session, agent=agent, phase="competition", bench_version=7
            )
            http.runs[str(old.run_id)]["state"] = "approved"
            await refresh_plan(state, session, old)
            await bind_grant(session, uuid4(), old)
            agent.status = "scored"
            await session.flush()
            session.info["paid_evaluation_required"] = True
            args = {
                "agent_id": aid,
                "validator_hotkey": "maintenance-validator",
                "now": datetime.now(UTC),
                "ttl": timedelta(minutes=30),
                "bench_version": 7,
            }
            assert await issue_confirmation_ticket(session, **args) is None
            fresh = await propose_plan(
                state,
                session,
                agent=agent,
                phase="competition",
                bench_version=7,
                explicit=True,
            )
            assert (
                old.state == "closed"
                and fresh.run_id != old.run_id
                and fresh.state == "proposed"
            )
            assert await issue_confirmation_ticket(session, **args) is None
            http.runs[str(fresh.run_id)]["state"] = "approved"
            await refresh_plan(state, session, fresh)
            ticket = await issue_confirmation_ticket(session, **args)
            assert ticket is not None
            await bind_grant(session, uuid4(), fresh)
            await session.flush()
            args["validator_hotkey"] = "next-maintenance-validator"
            assert await issue_confirmation_ticket(session, **args) is None


@pytest.mark.asyncio
async def test_operator_retest_waits_for_cap_created_after_its_request(session_maker):
    from ditto.db.queries.audit import EVENT_SCORE_RETEST_QUEUED
    from ditto.db.queries.score_retests import activate_next_score_retest
    from ditto.tests.db.queries.test_score_retests import (
        _HOTKEY,
        _NOW,
        _seed_contract_retest,
    )

    http = BillingHTTP()
    async with (
        httpx.AsyncClient(transport=httpx.MockTransport(http.handle)) as client,
        session_maker() as session,
        session.begin(),
    ):
        aid = uuid4()
        await _seed_contract_retest(
            session,
            agent_id=aid,
            run_id="preserved-score",
            event=EVENT_SCORE_RETEST_QUEUED,
            request_id=str(uuid4()),
            name="paid-retest",
        )
        agent = await linked(session, aid)
        state = SimpleNamespace(
            config=SimpleNamespace(
                evaluation_billing=EvaluationBillingConfig(
                    enabled=True, api_key="isolated-test"
                )
            ),
            inference_client=client,
        )
        old = await propose_plan(
            state, session, agent=agent, phase="competition", bench_version=9
        )
        http.runs[str(old.run_id)]["state"] = "approved"
        await refresh_plan(state, session, old)
        old.created_at = _NOW - timedelta(days=1)
        await bind_grant(session, uuid4(), old)
        await session.flush()
        session.info["paid_evaluation_required"] = True
        args = {
            "validator_hotkey": _HOTKEY,
            "now": datetime.now(UTC),
            "supports_version": lambda version: version == 9,
            "slot_id": "slot-1",
        }
        assert await activate_next_score_retest(session, **args) is None
        fresh = await propose_plan(
            state,
            session,
            agent=agent,
            phase="competition",
            bench_version=9,
            explicit=True,
        )
        http.runs[str(fresh.run_id)]["state"] = "approved"
        await refresh_plan(state, session, fresh)
        await session.flush()
        assert await activate_next_score_retest(session, **args) is not None


class BillingHTTP:
    def __init__(self):
        self.runs = {}
        self.operations = {}
        self.calls = []
        self.unknown = False

    def handle(self, request):
        import json

        body = json.loads(request.content)
        self.calls.append(
            (request.url.path, body, request.headers.get("x-ditto-on-behalf-of"))
        )
        if request.url.path.endswith("/runs"):
            run = self.runs.setdefault(
                body["runId"],
                {
                    **body,
                    "state": "proposed",
                    "expiresAt": (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
                },
            )
            return httpx.Response(200, json=run)
        if request.url.path.endswith("/close"):
            if self.unknown:
                return httpx.Response(
                    409, json={"message": "unknown cost remains held"}
                )
            self.runs[request.url.path.split("/")[-2]]["state"] = "closed"
            return httpx.Response(200, json={})
        if request.url.path.endswith("/reservations"):
            self.operations[body["id"]] = body
        return httpx.Response(200, json={})


async def linked(session, agent_id):
    agent = await session.get(Agent, agent_id)
    await upsert_link(
        session,
        hotkey=agent.miner_hotkey,
        ditto_user_id="payer-" + str(agent_id),
        ditto_email=None,
        miner_coldkey=None,
        linked_via="cli",
        session_id=None,
        now=datetime.now(UTC),
    )
    return agent


@pytest.mark.asyncio
async def test_unapproved_head_does_not_take_slot_or_starve_funded_submission(
    session_maker,
):
    http = BillingHTTP()
    async with (
        httpx.AsyncClient(transport=httpx.MockTransport(http.handle)) as client,
        session_maker() as session,
    ):
        state = SimpleNamespace(
            config=SimpleNamespace(
                evaluation_billing=EvaluationBillingConfig(
                    enabled=True, api_key="isolated-test"
                )
            ),
            inference_client=client,
        )
        now = datetime.now(UTC)
        first = await _seed_evaluating(
            session, name="unfunded", created_at=now - timedelta(minutes=1)
        )
        second = await _seed_evaluating(session, name="funded", created_at=now)
        async with session.begin():
            a, b = await linked(session, first), await linked(session, second)
            pending = await propose_plan(
                state, session, agent=a, phase="competition", bench_version=7
            )
            funded = await propose_plan(
                state, session, agent=b, phase="competition", bench_version=7
            )
            http.runs[str(funded.run_id)]["state"] = "approved"
            await refresh_plan(state, session, funded)
        session.info["paid_evaluation_required"] = True
        async with session.begin():
            ticket = await issue_ticket(
                session,
                validator_hotkey="5PaidValidator",
                now=now,
                ttl=timedelta(minutes=30),
                bench_version=7,
            )
            assert ticket is not None and ticket.agent_id == second
            assert (
                int(
                    await session.scalar(
                        select(func.count())
                        .select_from(ValidatorTicket)
                        .where(ValidatorTicket.agent_id == first)
                    )
                    or 0
                )
                == 0
            )
            assert pending.state == "proposed"


@pytest.mark.asyncio
async def test_expiry_releases_unused_cap_but_never_unknown_provider_hold(
    session_maker,
):
    http = BillingHTTP()
    async with (
        httpx.AsyncClient(transport=httpx.MockTransport(http.handle)) as client,
        session_maker() as session,
    ):
        state = SimpleNamespace(
            config=SimpleNamespace(
                evaluation_billing=EvaluationBillingConfig(
                    enabled=True, api_key="isolated-test"
                )
            ),
            inference_client=client,
        )
        aid = await _seed_evaluating(session, name="expiry")
        async with session.begin():
            agent = await linked(session, aid)
            plan = await propose_plan(
                state, session, agent=agent, phase="competition", bench_version=7
            )
            http.runs[str(plan.run_id)]["state"] = "approved"
            await refresh_plan(state, session, plan)
            plan.expires_at = datetime.now(UTC) - timedelta(seconds=1)
            http.unknown = True
            await reconcile_plan(state, session, plan)
            assert plan.state == "closing"
            http.unknown = False
            await reconcile_plan(state, session, plan)
            assert plan.state == "closed"
            fresh = await propose_plan(
                state,
                session,
                agent=agent,
                phase="competition",
                bench_version=7,
                explicit=True,
            )
            assert (
                fresh.run_id != plan.run_id
                and fresh.cycle == plan.cycle + 1
                and fresh.state == "proposed"
            )


@pytest.mark.asyncio
async def test_grant_binding_cannot_migrate_to_new_approved_cycle(session_maker):
    http = BillingHTTP()
    async with (
        httpx.AsyncClient(transport=httpx.MockTransport(http.handle)) as client,
        session_maker() as session,
    ):
        state = SimpleNamespace(
            config=SimpleNamespace(
                evaluation_billing=EvaluationBillingConfig(
                    enabled=True, api_key="isolated-test"
                )
            ),
            inference_client=client,
        )
        aid = await _seed_evaluating(session, name="binding")
        async with session.begin():
            agent = await linked(session, aid)
            prior = await propose_plan(
                state, session, agent=agent, phase="competition", bench_version=7
            )
            http.runs[str(prior.run_id)]["state"] = "approved"
            await refresh_plan(state, session, prior)
            grant = uuid4()
            await bind_grant(session, grant, prior)
            prior.state = "closed"
            await session.flush()
            current = await propose_plan(
                state, session, agent=agent, phase="competition", bench_version=7
            )
            with pytest.raises(HTTPException) as error:
                await bind_grant(session, grant, current)
            assert error.value.status_code == 409
            with pytest.raises(HTTPException) as error:
                await plan_for_grant(session, grant)
            assert error.value.status_code == 402


@pytest.mark.asyncio
async def test_all_stage_clients_use_bound_budget_and_unknown_transport_stays_held(
    session_maker,
):
    http = BillingHTTP()
    async with (
        httpx.AsyncClient(transport=httpx.MockTransport(http.handle)) as client,
        session_maker() as session,
    ):
        state = SimpleNamespace(
            config=SimpleNamespace(
                evaluation_billing=EvaluationBillingConfig(
                    enabled=True, api_key="isolated-test"
                )
            ),
            inference_client=client,
        )
        request = SimpleNamespace(app=SimpleNamespace(state=state))
        aid = await _seed_evaluating(session, name="stages")
        async with session.begin():
            agent = await linked(session, aid)
            plan = await propose_plan(
                state, session, agent=agent, phase="competition", bench_version=7
            )
            http.runs[str(plan.run_id)]["state"] = "approved"
            await refresh_plan(state, session, plan)
            grant = uuid4()
            await bind_grant(session, grant, plan)
            await session.flush()
            paid = await begin_paid_evaluation(
                request,
                session,
                agent_id=aid,
                grant_id=grant,
                nonce=uuid4(),
                stage="competition-embedding",
                model="perplexity/pplx-embed-v1-0.6b",
                provider="perplexity",
                body=b"trusted request",
            )
            assert paid is not None
            await paid.finish(response=None)
            assert http.calls[-1][1] == {"state": "unknown"}
            paid = PaidEvaluationCall(
                request,
                state.config.evaluation_billing,
                plan.payer_uid,
                str(uuid4()),
                "Frozen",
            )
            await paid.finish(
                response=httpx.Response(200, json={"id": "provider-receipt"}),
                input_tokens=12,
                output_tokens=4,
                cost_microusd=25,
            )
            assert http.calls[-1][1]["actualMicrousd"] == 25
            assert http.calls[-1][1]["state"] == "settled"
            assert http.calls[-1][2] == plan.payer_uid


@pytest.mark.asyncio
async def test_shared_confirmation_requires_explicit_candidate_payer_and_whole_quote(
    session_maker,
):
    from ditto.db.models import ConfirmationBundleSubject
    from ditto.tests.confirmation_evidence_fixtures import verification_profile
    from ditto.tests.db.queries.test_confirmation_bundles import seed_agent, seed_bundle

    http = BillingHTTP()
    profile = verification_profile()
    async with (
        httpx.AsyncClient(transport=httpx.MockTransport(http.handle)) as client,
        session_maker() as session,
        session.begin(),
    ):
        aid, _, _, bundle = await seed_bundle(session, name="candidate")
        incumbent = await seed_agent(session, name="incumbent")
        # Production deduplicates this comparison across subjects; attach the
        # second immutable subject with the same base proof as the candidate.
        original = await session.get(ConfirmationBundleSubject, (aid, 9))
        values = {
            c.name: getattr(original, c.name)
            for c in ConfirmationBundleSubject.__table__.columns
            if c.name not in {"agent_id", "created_at"}
        }
        session.add(ConfirmationBundleSubject(**values, agent_id=incumbent))
        candidate = await linked(session, aid)
        await linked(session, incumbent)
        await session.flush()
        state = SimpleNamespace(
            config=SimpleNamespace(
                evaluation_billing=EvaluationBillingConfig(
                    enabled=True, api_key="isolated-test"
                )
            ),
            inference_client=client,
            confirmation_verification_profiles={
                (bundle.profile_revision, bundle.profile_checksum): profile
            },
        )
        with pytest.raises(HTTPException) as error:
            await propose_plan(
                state,
                session,
                agent=candidate,
                phase="confirmation",
                bench_version=9,
                bundle_id=bundle.bundle_id,
            )
        assert error.value.status_code == 402 and not http.calls
        plan = await propose_plan(
            state,
            session,
            agent=candidate,
            phase="confirmation",
            bench_version=9,
            bundle_id=bundle.bundle_id,
            explicit=True,
        )
        whole = (
            sum(lane.max_cost_usd_micros for lane in profile.provider_lanes)
            + profile.embedding_lane.max_cost_usd_micros
        )
        assert plan.payer_uid == "payer-" + str(aid) and plan.estimate_microusd >= whole
        assert (
            http.calls[-1][1]["phase"] == "confirmation"
            and http.calls[-1][1]["estimateMicrousd"] >= whole
        )
        assert plan.state == "proposed"  # Binding alone never authorizes dispatch.
        other = await session.get(Agent, incumbent)
        with pytest.raises(HTTPException) as error:
            await propose_plan(
                state,
                session,
                agent=other,
                phase="confirmation",
                bench_version=9,
                bundle_id=bundle.bundle_id,
                explicit=True,
            )
        assert error.value.status_code == 409
