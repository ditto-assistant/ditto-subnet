"""Fixed-cap credit accounting around the existing frozen provider routes.

Provider dispatch remains in inference.py. This client never routes inference
through the product Router and therefore cannot double-debit its user wallet.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
from dataclasses import dataclass
from urllib.parse import urlparse
from uuid import UUID

import httpx
from fastapi import HTTPException, Request
from sqlalchemy import select

from ditto.db.models import Agent, MinerDittoLink


@dataclass(frozen=True)
class EvaluationBillingConfig:
    enabled: bool = False
    url: str = "https://api.heyditto.ai/v1/evaluation-billing"
    api_key: str = ""
    estimate_microusd: int = 1_000_000
    cap_microusd: int = 5_000_000
    request_microusd: int = 1_000_000


def parse_evaluation_billing_config() -> EvaluationBillingConfig:
    try:
        config = EvaluationBillingConfig(
            enabled=os.environ.get("DITTO_EVALUATION_BILLING_ENABLED", "false")
            .strip()
            .lower()
            in {"true", "1", "yes"},
            url=os.environ.get(
                "DITTO_EVALUATION_BILLING_URL",
                "https://api.heyditto.ai/v1/evaluation-billing",
            ),
            api_key=os.environ.get("DITTO_ROUTER_API_KEY", ""),
            estimate_microusd=int(
                os.environ.get("DITTO_EVALUATION_ESTIMATE_MICROUSD", "1000000")
            ),
            cap_microusd=int(
                os.environ.get("DITTO_EVALUATION_CAP_MICROUSD", "5000000")
            ),
            request_microusd=int(
                os.environ.get("DITTO_EVALUATION_REQUEST_MICROUSD", "1000000")
            ),
        )
    except ValueError as error:
        raise ValueError("evaluation billing limits must be integers") from error
    if config.enabled:
        parsed = urlparse(config.url)
        host = parsed.hostname or ""
        trusted = host in {
            "api.heyditto.ai",
            "router.heyditto.ai",
            "inference.heyditto.ai",
            "staging-api.heyditto.ai",
        } or (
            host.endswith("-api.heyditto.ai")
            and "." not in host.removesuffix("-api.heyditto.ai")
        )
        if (
            not trusted
            or parsed.scheme != "https"
            or parsed.path != "/v1/evaluation-billing"
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
            or parsed.port
        ):
            raise ValueError(
                "evaluation billing URL must be a trusted Ditto HTTPS origin"
            )
        if (
            not config.api_key
            or not 0 < config.estimate_microusd <= config.cap_microusd <= 50_000_000
            or not 0 < config.request_microusd <= config.cap_microusd
        ):
            raise ValueError(
                "evaluation billing requires a server key and bounded limits"
            )
    return config


def billing_enabled(request: Request) -> bool:
    return getattr(
        request.app.state.config, "evaluation_billing", EvaluationBillingConfig()
    ).enabled


async def _post(
    client: httpx.AsyncClient,
    config: EvaluationBillingConfig,
    user: str,
    path: str,
    payload: dict,
) -> dict:
    try:
        response = await client.post(
            config.url + path,
            json=payload,
            headers={
                "Authorization": "Bearer " + config.api_key,
                "X-Ditto-On-Behalf-Of": user,
                "X-Ditto-Require-User-Billing": "true",
            },
            timeout=15,
            follow_redirects=False,
        )
    except httpx.HTTPError as error:
        raise HTTPException(
            503, "Evaluation billing is temporarily unavailable"
        ) from error
    if response.status_code != 200:
        status = response.status_code if response.status_code in {402, 409} else 503
        raise HTTPException(
            status,
            "Approve this evaluation budget in Ditto and check available credits",
        )
    if len(response.content) > 65536:
        raise HTTPException(503, "Evaluation billing is temporarily unavailable")
    try:
        result = response.json()
    except ValueError as error:
        raise HTTPException(
            503, "Evaluation billing is temporarily unavailable"
        ) from error
    if not isinstance(result, dict):
        raise HTTPException(503, "Evaluation billing is temporarily unavailable")
    return result


async def linked_user(session, agent_id: UUID) -> str:
    uid = await session.scalar(
        select(MinerDittoLink.ditto_user_id)
        .join(Agent, Agent.miner_hotkey == MinerDittoLink.miner_hotkey)
        .where(Agent.agent_id == agent_id, MinerDittoLink.revoked_at.is_(None))
    )
    if not uid:
        raise HTTPException(
            402, "Sign in with Ditto and link your miner account before evaluation"
        )
    return uid


async def confirmation_billing_agent(session, bundle_id: UUID, grant_id: UUID) -> UUID:
    from ditto.api_server.paid_evaluation_plans import plan_for_grant

    plan = await plan_for_grant(session, grant_id)
    if plan.phase != "confirmation" or plan.bundle_id != bundle_id:
        raise HTTPException(
            402, "Confirmation needs its explicitly approved payer budget"
        )
    return plan.agent_id


@dataclass
class PaidEvaluationCall:
    request: Request
    config: EvaluationBillingConfig
    user: str
    operation_id: str
    provider: str
    settled: bool = False

    async def finish(
        self,
        *,
        response: httpx.Response | None,
        input_tokens: int = 0,
        output_tokens: int = 0,
        cost_microusd: int | None = None,
    ) -> None:
        if self.settled:
            return
        payload: dict = {"state": "unknown"}
        if response is not None and cost_microusd is not None:
            try:
                decoded = response.json()
            except ValueError:
                decoded = {}
            provider_id = decoded.get("id") if isinstance(decoded, dict) else None
            if not isinstance(provider_id, str) or not provider_id:
                provider_id = (
                    "usage:"
                    + self.operation_id
                    + ":"
                    + hashlib.sha256(response.content).hexdigest()
                )
            payload = {
                "state": "settled",
                "provider": decoded.get("provider", self.provider)
                if isinstance(decoded, dict)
                else self.provider,
                "actualMicrousd": max(0, cost_microusd),
                "providerReceiptId": self.provider + ":" + provider_id,
                "inputTokens": input_tokens,
                "outputTokens": output_tokens,
            }
        # Shield a bounded HTTP operation so broker cancellation cannot free
        # an uncertain provider call. Any failure leaves the durable hold intact.
        task = asyncio.create_task(
            _post(
                self.request.app.state.inference_client,
                self.config,
                self.user,
                "/operations/" + self.operation_id + "/settle",
                payload,
            )
        )
        await asyncio.shield(task)
        self.settled = True


async def begin_paid_evaluation(
    request: Request,
    session,
    *,
    agent_id: UUID,
    grant_id: UUID,
    nonce: UUID,
    stage: str,
    model: str,
    provider: str,
    body: bytes,
    run_id: UUID | None = None,
) -> PaidEvaluationCall | None:
    config = getattr(
        request.app.state.config, "evaluation_billing", EvaluationBillingConfig()
    )
    if not config.enabled:
        return None
    from ditto.api_server.paid_evaluation_plans import plan_for_grant

    plan = await plan_for_grant(session, grant_id)
    user = await linked_user(session, agent_id)
    if (
        plan.agent_id != agent_id
        or plan.payer_uid != user
        or plan.phase
        != ("confirmation" if stage.startswith("confirmation-") else "competition")
    ):
        raise HTTPException(
            402, "This evaluation budget belongs to a different payer or phase"
        )
    run_id = plan.run_id
    await _post(
        request.app.state.inference_client,
        config,
        user,
        "/reservations",
        {
            "runId": str(run_id),
            "id": str(nonce),
            "grantId": str(grant_id),
            "stage": stage,
            "model": model,
            "provider": provider,
            "requestSha256": hashlib.sha256(body).hexdigest(),
            "maxCostMicrousd": config.request_microusd,
        },
    )
    return PaidEvaluationCall(request, config, user, str(nonce), provider)
