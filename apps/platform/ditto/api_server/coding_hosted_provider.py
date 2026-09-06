"""Private native provider adapter. Not an HTTP route or a worker activation."""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
from dataclasses import dataclass
from decimal import ROUND_CEILING, Decimal, localcontext
from typing import Protocol
from uuid import UUID

import httpx

from ditto.api_models.coding_hosted_inference import (
    HostedInferencePolicy,
    HostedInferenceSettlement,
)
from ditto.api_models.coding_hosted_relay import HostedRelayBinding
from ditto.api_models.coding_inference import (
    CodingInferenceProviderResponse,
    _decode_json_document,
    coding_inference_canonical_json_bytes,
    parse_coding_inference_json,
)
from ditto.api_server.coding_hosted_budget import ProfiledBudgetEstimator
from ditto.api_server.coding_hosted_inference import (
    DispatchReservation,
    HostedInferenceLedger,
    ReservationCeilings,
)

ENDPOINT = "https://openrouter.ai/api/v1/chat/completions"
MAX_RESPONSE_BYTES = 8 << 20


class HostedProviderError(ValueError):
    """Safe class-only failure; never provider text, keys or request bodies."""


class BudgetEstimator(Protocol):
    """Synthetic test seam; network transport requires ProfiledBudgetEstimator."""

    def ceilings(self, canonical_request: bytes) -> ReservationCeilings: ...


@dataclass(frozen=True, repr=False)
class ProviderResult:
    response: bytes
    settlement: HostedInferenceSettlement
    provider_evidence: bytes


def verify_response(
    body: bytes, policy: HostedInferencePolicy, reservation: DispatchReservation
) -> ProviderResult:
    """Verify a response obtained over the adapter's authenticated TLS channel.

    Metadata is provider-attested, not independent proof of region, account
    guardrails or physical execution. Those still require a reviewed live profile.
    """
    try:
        payload = _decode_json_document(body, maximum_bytes=MAX_RESPONSE_BYTES)
        if not isinstance(payload, dict) or "error" in payload:
            raise ValueError
        metadata = payload.get("openrouter_metadata")
        if not isinstance(metadata, dict):
            raise ValueError
        # Inspect known security fields; advisory/additive metadata is ignored.
        # Require positive evidence: absent pipeline/attempt facts are not safe.
        if (
            metadata.get("requested") != policy.model
            or metadata.get("strategy") != "direct"
            or type(metadata.get("attempt")) is not int
            or metadata["attempt"] != 1
            or metadata.get("pipeline") != []
            or metadata.get("is_byok") is not False
        ):
            raise ValueError
        endpoints = metadata.get("endpoints")
        if (
            not isinstance(endpoints, dict)
            or type(endpoints.get("total")) is not int
            or endpoints["total"] != 1
        ):
            raise ValueError
        expected_endpoint = {
            "provider": policy.receipt_provider,
            "model": policy.model,
            "selected": True,
        }
        available = endpoints.get("available")
        if not isinstance(available, list) or len(available) != 1:
            raise ValueError
        endpoint = available[0]
        if (
            not isinstance(endpoint, dict)
            or any(endpoint.get(k) != v for k, v in expected_endpoint.items())
            or endpoint.get("selected") is not True
        ):
            raise ValueError
        # If the provider exposes its attempt log, independently check it too.
        if "attempts" in metadata:
            attempts = metadata["attempts"]
            if not isinstance(attempts, list) or len(attempts) != 1:
                raise ValueError
            attempt = attempts[0]
            if (
                not isinstance(attempt, dict)
                or attempt.get("provider") != policy.receipt_provider
                or attempt.get("model") != policy.model
                or type(attempt.get("status")) is not int
                or attempt["status"] != 200
            ):
                raise ValueError
        choices = payload.get("choices")
        if not isinstance(choices, list) or len(choices) != 1:
            raise ValueError
        choice = choices[0]
        if not isinstance(choice, dict) or "error" in choice:
            raise ValueError
        message = choice.get("message")
        if not isinstance(message, dict) or message.get("role") != "assistant":
            raise ValueError
        usage = payload.get("usage")
        if not isinstance(usage, dict) or type(usage.get("cost")) not in (int, float):
            raise ValueError
        parsed = parse_coding_inference_json(
            CodingInferenceProviderResponse, body, maximum_bytes=MAX_RESPONSE_BYTES
        )
        if parsed.provider != policy.receipt_provider:
            raise ValueError
        # Decimal arithmetic, rounded UP: never under-account a sub-micro charge.
        # Pydantic's JSON-to-Decimal path passes through binary floating point.
        # Re-read only billing from the already bounded/duplicate-checked JSON
        # with Decimal, so a tiny excess above a whole micro is not rounded away.
        exact_cost = Decimal(json.loads(body, parse_float=Decimal)["usage"]["cost"])
        if not exact_cost.is_finite() or not 0 <= exact_cost <= 100:
            raise ValueError
        with localcontext() as context:
            context.prec = 96  # JSON number spellings are bounded to 64 chars.
            micros = int(
                (exact_cost * Decimal(1_000_000)).to_integral_value(
                    rounding=ROUND_CEILING
                )
            )
        response = coding_inference_canonical_json_bytes(
            {
                "schema": "dittobench-coding-hosted-inference-response-v2",
                "id": parsed.id,
                "model": parsed.model,
                "provider": parsed.provider,
                "choices": [value.model_dump(mode="json") for value in parsed.choices],
                "usage": {
                    "prompt_tokens": parsed.usage.prompt_tokens,
                    "completion_tokens": parsed.usage.completion_tokens,
                    "total_tokens": parsed.usage.total_tokens,
                    "cost_usd_micros": micros,
                },
            }
        )
        # Generation identity, not the mutable response body, deduplicates
        # receipts across requests. A replay with altered text/usage cannot earn
        # a second settlement. Raw provider bytes never enter the database.
        receipt_identity = coding_inference_canonical_json_bytes(
            {"provider_api": "openrouter", "generation_id": parsed.id}
        )
        settlement = HostedInferenceSettlement(
            schema="dittobench-coding-hosted-inference-settlement-v2",
            evaluation_id=reservation.evaluation_id,
            attempt_id=reservation.attempt_id,
            grant_id=reservation.grant_id,
            request_id=reservation.request_id,
            sequence=reservation.sequence,
            policy_sha256=reservation.policy_sha256,
            locked_request_sha256=reservation.locked_request_sha256,
            response_sha256=hashlib.sha256(response).hexdigest(),
            provider_receipt_sha256=hashlib.sha256(receipt_identity).hexdigest(),
            model=policy.model,
            provider=policy.receipt_provider,
            provider_route=policy.provider_route,
            provider_route_profile=policy.provider_route_profile,
            fallback_used=False,
            prompt_tokens=parsed.usage.prompt_tokens,
            completion_tokens=parsed.usage.completion_tokens,
            cost_usd_micros=micros,
        )
        if reservation.policy_sha256 != policy.digest():
            raise ValueError
        # Separate private evidence for the future sealed-evidence publisher;
        # only `response` may be projected through a miner-facing relay.
        return ProviderResult(response, settlement, bytes(body))
    except (ValueError, TypeError, ArithmeticError):
        raise HostedProviderError("hosted provider response is unverified") from None


class HostedProviderAdapter:
    """One private grant; caller must be the trusted Platform worker.

    This is NOT a miner-facing handler. No request header, endpoint selector or
    caller usage estimate is accepted. A later source-bound relay must lock the
    miner request and authenticate its container before calling this object.
    """

    def __init__(
        self,
        *,
        ledger: HostedInferenceLedger,
        grant_id: UUID,
        policy: HostedInferencePolicy,
        estimator: BudgetEstimator,
        api_key: str,
        _test_transport: httpx.MockTransport | None = None,
    ):
        if (
            not isinstance(grant_id, UUID)
            or not grant_id.int
            or not isinstance(api_key, str)
            or not 1 <= len(api_key) <= 4096
            or any(not 33 <= ord(char) <= 126 for char in api_key)
            or not callable(getattr(estimator, "ceilings", None))
            or (
                _test_transport is not None
                and type(_test_transport) is not httpx.MockTransport
            )
        ):
            raise HostedProviderError("hosted provider configuration is invalid")
        self._ledger, self._grant = ledger, grant_id
        self._policy = HostedInferencePolicy.model_validate_json(
            policy.model_dump_json(by_alias=True)
        )
        if isinstance(estimator, ProfiledBudgetEstimator):
            estimator.require_policy(self._policy)
        elif _test_transport is None or self._policy.runtime_profile_sha256 is not None:
            raise HostedProviderError("a policy-bound runtime estimator is required")
        self._estimator, self._key = estimator, api_key
        self._test_transport = _test_transport
        self._busy = False
        self._closed = False

    async def complete(
        self, *, request_id: UUID, locked_request: bytes
    ) -> ProviderResult:
        if self._closed or self._busy:
            raise HostedProviderError("hosted provider admission is closed or busy")
        self._busy = True
        try:
            parsed, digest = self._policy.locked_request(locked_request)
            canonical = coding_inference_canonical_json_bytes(parsed)
            ceilings = self._estimator.ceilings(canonical)
            reservation = await self._ledger.reserve(
                grant_id=self._grant,
                request_id=request_id,
                locked_request=canonical,
                ceilings=ceilings,
            )
            if (
                self._closed
                or not reservation.newly_reserved
                or reservation.policy_sha256 != self._policy.digest()
                or reservation.locked_request_sha256 != digest
            ):
                raise HostedProviderError("hosted provider dispatch is not fresh")
            timeout = min(
                self._policy.request_timeout_milliseconds / 1000,
                reservation.expires_at_unix - time.time(),
            )
            if isinstance(self._estimator, ProfiledBudgetEstimator):
                # A DB transaction may outlast profile validity. Recheck after
                # commit, before provider I/O, and cap the call by profile expiry.
                timeout = min(timeout, self._estimator.remaining_seconds())
            if timeout <= 0:
                raise HostedProviderError("hosted provider deadline expired")
            async with asyncio.timeout(timeout):
                body = await self._post(canonical, timeout)
            result = verify_response(body, self._policy, reservation)
            if isinstance(self._estimator, ProfiledBudgetEstimator):
                self._estimator.verify_usage(result.settlement)
            # Release no model output until durable, fully bound accounting has
            # committed. Lost acknowledgement is terminal, never a fresh retry.
            await self._ledger.settle(result.settlement)
            await self._ledger.require_active(self._grant)
            if isinstance(self._estimator, ProfiledBudgetEstimator):
                self._estimator.remaining_seconds()
            if self._closed or time.time() >= reservation.expires_at_unix:
                raise HostedProviderError("hosted provider output is no longer active")
            return result
        except asyncio.CancelledError:
            self._closed = True
            # Reserved rows continue to block freeze. The caller must await the
            # cancelled task and revoke the ledger; closing TLS is not proof the
            # remote provider has stopped execution or billing.
            raise
        except Exception:
            self._closed = True
            raise HostedProviderError(
                "hosted provider attempt did not complete"
            ) from None
        finally:
            self._busy = False

    async def authorize_relay(self, binding: HostedRelayBinding) -> None:
        if isinstance(self._estimator, ProfiledBudgetEstimator):
            self._estimator.require_policy(self._policy)
        if (
            self._closed
            or binding.grant_id != self._grant
            or binding.policy_sha256 != self._policy.digest()
        ):
            raise HostedProviderError("hosted relay authority is unavailable")
        await self._ledger.require_relay_binding(binding)

    async def complete_miner(
        self, *, request_id: UUID, miner_request: bytes
    ) -> ProviderResult:
        try:
            locked = self._policy.lock_miner_request(miner_request)
        except ValueError:
            raise HostedProviderError("hosted miner request violates policy") from None
        return await self.complete(request_id=request_id, locked_request=locked)

    async def revoke(self) -> bool:
        """Close admission; Boolean describes ONLY the durable ledger drain.

        Does not cancel an in-flight call, attest provider quiescence, mark an
        uncertain call resolved, or delete a pending reservation. The worker owns
        task cancellation/drain and crash recovery before any patch freeze.
        """
        self._closed = True
        return await self._ledger.revoke(self._grant)

    async def _post(self, body: bytes, timeout: float) -> bytes:
        # Own the transport: no ambient proxies, cookies, auth, redirects or
        # retries; no caller URL/headers. Test injection is private Python only.
        transport = self._test_transport or httpx.AsyncHTTPTransport(
            retries=0, trust_env=False
        )
        async with (
            httpx.AsyncClient(
                transport=transport,
                trust_env=False,
                follow_redirects=False,
                timeout=timeout,
            ) as client,
            client.stream(
                "POST",
                ENDPOINT,
                content=body,
                headers={
                    "Authorization": "Bearer " + self._key,
                    "Content-Type": "application/json",
                    "Accept-Encoding": "identity",
                    "X-OpenRouter-Metadata": "enabled",
                    "X-OpenRouter-Cache": "false",
                },
            ) as response,
        ):
            if (
                response.status_code != 200
                or response.headers.get("content-type", "")
                .split(";")[0]
                .strip()
                .lower()
                != "application/json"
                or response.headers.get("content-encoding", "identity") != "identity"
            ):
                raise HostedProviderError("hosted provider transport rejected")
            output = bytearray()
            async for chunk in response.aiter_raw():
                if len(output) + len(chunk) > MAX_RESPONSE_BYTES:
                    raise HostedProviderError("hosted provider response exceeds bound")
                output.extend(chunk)
            return bytes(output)
