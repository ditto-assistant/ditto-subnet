"""Dark-launchable ticket-scoped OpenRouter inference plane.

Only a validator-authenticated exchange can bind a grant to a trusted local
broker key. Every inference call then requires the opaque grant secret plus an
Ed25519 proof over the exact request body. Request bodies and provider payloads
are intentionally never logged.

How a refusal is signalled, and why it looks like this
------------------------------------------------------

``429`` used to mean three unrelated things here: the lease was revoked, the
lease's budget was spent, or the lane was simply full. The broker
(``dittobench-api``) classifies purely on status, so all three read as "lease
gone, discard the run". That cost real runs -- ``banblackycat`` died to 17
capacity declines against a perfectly healthy lease.

The fix is not "more status codes". Status codes are a two-bit channel and this
is application semantics, so the authoritative signal is the numeric
``error_code`` already present in every error body (see
``middleware/error_envelope.py``, 41xx). The status is kept as a coarse,
*honest* hint:

* retryable  -> ``503`` + ``Retry-After``
* terminal   -> ``429``

Choosing ``503`` for the retryable case is the entire backward-compatibility
story, and it is forced rather than chosen. A broker pinned to an old build
sees only the status, so:

* ``503`` already falls in its transient class -- it backs off, retries, and on
  final failure reports the same ``502`` to the harness it always did. Strictly
  better than today, where the same event ends the run instantly.
* ``409``/``403`` would be *worse* than today. The old broker forwards an
  unrecognised 4xx to the harness verbatim, changing the gateway contract
  mid-benchmark -- the one thing its own comments say must never happen.

So revocation and exhaustion keep ``429`` (which old brokers already treat
correctly, as fatal) and only the retryable case moves. New brokers read
``error_code`` and can finally tell revocation from exhaustion; old brokers see
no regression on any path. The embedding lane has answered this way since
dittobench-api #103 and the fleet already understands it.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import math
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Annotated, Any
from uuid import UUID

import httpx
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from fastapi import APIRouter, Header, HTTPException, Request, Response

from ditto.api_models.inference import (
    InferenceExchangeRequest,
    InferenceExchangeResponse,
)
from ditto.api_server.endpoints.validator import (
    ChainDep,
    SessionDep,
    ValidatorAuthError,
    _assert_validator_permitted,
    _verify_signature,
)
from ditto.api_server.inference_concurrency_settings import (
    resolved_proxy_config,
)
from ditto.api_server.inference_routing import (
    BENCH_V9_DEFAULT_REASONING_EFFORT,
    BENCH_V9_REASONING_EFFORTS,
    benchmark_reasoning,
    record_route_observation,
)
from ditto.db.queries.inference import (
    InferenceDecline,
    activate_inference_grant,
    begin_inference_request,
    finish_inference_request,
)
from ditto.db.queries.validator_auth import (
    ValidatorRequestReplayError,
    consume_validator_nonce,
)

if TYPE_CHECKING:
    from ditto.api_server.config import InferenceProxyConfig

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/inference", tags=["inference"])
_EXCHANGE_MAX_AGE = timedelta(minutes=2)
_PROXY_MAX_AGE = timedelta(seconds=30)
_EMBEDDING_MAX_INPUTS = 256
_PPLX_EMBED_CONTRACT_MODEL = "perplexity/pplx-embed-v1-0.6b"
_PPLX_EMBED_RESPONSE_MODEL = "pplx-embed-v1-0.6b"
_MIN_HOSTED_EMBEDDING_BENCH_VERSION = 7
_PROVIDER_MAX_ATTEMPTS = 3
_PROVIDER_RETRY_STATUSES = frozenset({408, 429, 500, 502, 503, 504})

# Bytes per token, for turning a request body into a token estimate.
#
# The reservation used to be ``max_tokens + len(body)`` -- byte length used
# directly as a token count. That is a genuine upper bound (a token cannot
# consume less than one byte of UTF-8) but it is roughly 4x the truth for the
# JSON this lane actually carries, and it is not merely reserved: every path
# that reclaims an unsettled request *charges* it as ``prompt_tokens``, so a
# timeout or a stale sweep books ~4x what the call really cost.
#
# 4 is measured against this lane's own traffic rather than picked: the
# calibration fleet's chat prompts run ~2,300 tokens against bodies in the
# 8-10KB range under the o200k-family tokenizer that ``openai/gpt-oss-20b``
# uses. English prose sits near 4, JSON escaping and code push it slightly
# under, so this stays a mild over-estimate in the ordinary case.
#
# Being approximately right is safe here, and the reason is worth stating: the
# estimate governs *admission* only. The real allowance is enforced against
# real settled usage, both by ``begin_inference_request`` (which compares the
# provider-reported spend) and by ``finish_inference_request`` (``spent >=
# token_budget`` -> exhausted). An estimate that runs low can overshoot the
# budget by at most one call per concurrent slot, never systematically.
_ESTIMATED_BYTES_PER_TOKEN = 4


def _estimated_tokens(body: bytes) -> int:
    """A realistic token estimate for a request body, floored at 1."""
    return max(1, -(-len(body) // _ESTIMATED_BYTES_PER_TOKEN))


def _uses_hosted_embeddings(bench_version: int) -> bool:
    """Whether a benchmark contract uses the ticket-scoped embedding lane.

    Hosted embeddings are an era boundary, not an enumerated allowlist. V7
    introduced the Platform-owned contract and every later benchmark inherits
    it unless a future version explicitly defines another lane. Keeping this
    as a lower-bound predicate prevents a newly supported scorer version from
    being admitted at the broker but rejected here by a stale version set.
    """

    return bench_version >= _MIN_HOSTED_EMBEDDING_BENCH_VERSION


def _max_chargeable_tokens(body: bytes, *, output_tokens: int = 0) -> int:
    """The tokenizer-independent hard ceiling on what this call may be charged.

    This is the old reservation figure, and it keeps that job: bounding
    *untrusted provider accounting*. A token cannot consume less than one byte
    of the UTF-8 body, so no honest ``usage`` block can exceed it.

    It is deliberately a second number rather than a reuse of the reservation.
    Once the reservation is an estimate, a legitimate request can exceed it by a
    little -- and ``finish_inference_request`` marks anything over its bound
    non-deliverable, which would turn ordinary token-dense prompts into 409s.
    Two numbers, two jobs: the estimate decides what the budget holds, the
    ceiling decides what a provider is allowed to claim.
    """
    return output_tokens + max(1, len(body))


class InferenceDeclinedError(Exception):
    """An admission refusal, carrying the machine-readable reason.

    Raised instead of ``HTTPException`` so the reason reaches the client as a
    stable numeric code instead of being flattened into a status the broker then
    has to guess from. Every refusal carries a decline now, including the
    deliberately-unexplained one (``InferenceDecline.UNATTRIBUTED``) -- there is
    no longer a ``None`` case meaning "we did not say".

    The status, the error code, and the ``Retry-After`` hint are all derived in
    ``middleware/error_envelope.py`` -- the numeric registry lives there, and
    importing it back into an endpoint would close an import cycle.
    """

    def __init__(self, decline: InferenceDecline, *, lane: str) -> None:
        super().__init__(f"{lane} inference declined: {decline}")
        self.decline = decline
        self.lane = lane


@dataclass(frozen=True)
class _ProviderResult:
    response: httpx.Response
    attempts: int


class _ProviderCallError(Exception):
    def __init__(self, *, attempts: int, timed_out: bool) -> None:
        super().__init__("provider request failed")
        self.attempts = attempts
        self.timed_out = timed_out


@dataclass(frozen=True)
class _ChatCompletionResult:
    raw: bytes
    usage: tuple[int, int, int]
    upstream_provider: str
    upstream_attempts: int
    openrouter_attempts: int
    fallback_phase: int


class _ChatProviderExhausted(Exception):
    """All reviewed provider phases failed without a deliverable completion."""

    def __init__(
        self,
        *,
        upstream_attempts: int,
        openrouter_attempts: int,
        fallback_phase: int,
        terminal_error_code: str,
        timed_out: bool,
        upstream_provider: str | None,
        route_observable: bool,
    ) -> None:
        super().__init__(terminal_error_code)
        self.upstream_attempts = upstream_attempts
        self.openrouter_attempts = openrouter_attempts
        self.fallback_phase = fallback_phase
        self.terminal_error_code = terminal_error_code
        self.timed_out = timed_out
        self.upstream_provider = upstream_provider
        self.route_observable = route_observable


async def _post_provider_with_retry(
    client: httpx.AsyncClient,
    url: str,
    *,
    payload: dict[str, Any],
    headers: dict[str, str],
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> _ProviderResult:
    """Run one logical provider request under the shared bounded retry policy.

    Only connection establishment failures and explicit transient HTTP statuses
    are safe to repeat here. A read failure is ambiguous: the provider may have
    completed and billed the request, so the caller fails closed and lets the
    validator retry the whole benchmark later instead of duplicating execution.
    """
    for attempt in range(1, _PROVIDER_MAX_ATTEMPTS + 1):
        try:
            response = await client.post(url, json=payload, headers=headers)
        except (httpx.ConnectError, httpx.ConnectTimeout) as error:
            if attempt == _PROVIDER_MAX_ATTEMPTS:
                raise _ProviderCallError(
                    attempts=attempt,
                    timed_out=isinstance(error, httpx.TimeoutException),
                ) from error
            await sleep(0.25 * (2 ** (attempt - 1)))
            continue
        except httpx.TimeoutException as error:
            raise _ProviderCallError(attempts=attempt, timed_out=True) from error
        except httpx.TransportError as error:
            raise _ProviderCallError(attempts=attempt, timed_out=False) from error
        if (
            response.status_code in _PROVIDER_RETRY_STATUSES
            and attempt < _PROVIDER_MAX_ATTEMPTS
        ):
            await response.aclose()
            await sleep(0.25 * (2 ** (attempt - 1)))
            continue
        return _ProviderResult(response=response, attempts=attempt)
    raise AssertionError("provider retry loop exhausted without a terminal result")


def _exchange_message(payload: InferenceExchangeRequest) -> bytes:
    requested = payload.requested_at.astimezone(UTC).isoformat(timespec="microseconds")
    return (
        f"validator-inference:v1:{payload.validator_hotkey}:{payload.grant_id}:"
        f"{payload.broker_public_key.rstrip('=')}:{payload.nonce}:{requested}"
    ).encode()


def _proxy_message(
    *, grant_id: UUID, generation: int, nonce: UUID, requested_at: datetime, body: bytes
) -> bytes:
    requested = requested_at.astimezone(UTC).isoformat(timespec="microseconds")
    digest = hashlib.sha256(body).hexdigest()
    return (
        f"ditto-inference:v1:{grant_id}:{generation}:{nonce}:{requested}:{digest}"
    ).encode()


def _decode_public_key(value: str) -> Ed25519PublicKey:
    try:
        raw = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
        return Ed25519PublicKey.from_public_bytes(raw)
    except (ValueError, TypeError) as error:
        raise HTTPException(
            status_code=401, detail="invalid inference proof"
        ) from error


def _bounded_usage(payload: dict[str, Any]) -> tuple[int, int, int] | None:
    usage = payload.get("usage")
    if not isinstance(usage, dict):
        return None
    prompt = usage.get("prompt_tokens")
    completion = usage.get("completion_tokens")
    if (
        not isinstance(prompt, int)
        or isinstance(prompt, bool)
        or prompt < 0
        or not isinstance(completion, int)
        or isinstance(completion, bool)
        or completion < 0
    ):
        return None
    if prompt + completion > (1 << 62):
        return None
    # Provider-supplied cost is intentionally ignored. The ticket pins catalog
    # prices and the trusted plane derives cost from validated token counts.
    return prompt, completion, 0


def _bounded_provider_cost(payload: dict[str, Any]) -> int | None:
    """Convert OpenRouter's direct response cost to bounded integer micro-USD."""
    usage = payload.get("usage")
    value = usage.get("cost") if isinstance(usage, dict) else None
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return None
    cost = float(value)
    if not math.isfinite(cost) or cost < 0 or cost > 100:
        return None
    return round(cost * 1_000_000)


def _upstream_provider(payload: dict[str, Any]) -> str | None:
    """Extract one actual provider from private OpenRouter routing metadata."""
    metadata = payload.get("openrouter_metadata")
    if metadata is None:
        # Bounded compatibility for older OpenRouter responses. Current
        # responses expose this only through opt-in router metadata, and cache
        # hits can intentionally omit that metadata.
        provider = payload.get("provider")
        return (
            provider
            if isinstance(provider, str) and 1 <= len(provider) <= 120
            else None
        )
    if not isinstance(metadata, dict):
        raise HTTPException(status_code=502, detail="provider identity mismatch")
    endpoints = metadata.get("endpoints")
    available = endpoints.get("available") if isinstance(endpoints, dict) else None
    if not isinstance(available, list):
        raise HTTPException(status_code=502, detail="provider identity mismatch")
    selected = [
        endpoint.get("provider")
        for endpoint in available
        if isinstance(endpoint, dict) and endpoint.get("selected") is True
    ]
    if (
        len(selected) != 1
        or not isinstance(selected[0], str)
        or not 1 <= len(selected[0]) <= 120
    ):
        raise HTTPException(status_code=502, detail="provider identity mismatch")
    return selected[0]


def _public_provider_response(payload: dict[str, Any]) -> dict[str, Any]:
    """Return only the normalized Chat Completions contract used by harnesses.

    OpenRouter's additive response surface includes provider-specific system
    fingerprints, service tiers, native finish reasons, error metadata, and
    opt-in routing details. None belongs across the untrusted harness boundary.
    """
    response_id = payload.get("id")
    created = payload.get("created")
    model = payload.get("model")
    choices = payload.get("choices")
    if (
        not isinstance(response_id, str)
        or not 1 <= len(response_id) <= 256
        or not isinstance(created, int)
        or isinstance(created, bool)
        or payload.get("object") != "chat.completion"
        or not isinstance(model, str)
        or not isinstance(choices, list)
        or len(choices) != 1
    ):
        raise HTTPException(status_code=502, detail="invalid provider response")

    choice = choices[0]
    if not isinstance(choice, dict) or "error" in choice:
        raise HTTPException(status_code=502, detail="inference provider unavailable")
    index = choice.get("index")
    finish_reason = choice.get("finish_reason")
    message = choice.get("message")
    if (
        not isinstance(index, int)
        or isinstance(index, bool)
        or finish_reason not in {"stop", "length", "tool_calls", "content_filter"}
        or not isinstance(message, dict)
        or message.get("role") != "assistant"
        or message.get("content") is not None
        and not isinstance(message.get("content"), str)
    ):
        raise HTTPException(status_code=502, detail="invalid provider response")

    public_message: dict[str, Any] = {
        "role": "assistant",
        "content": message.get("content"),
    }
    tool_calls = message.get("tool_calls")
    if tool_calls is not None:
        if not isinstance(tool_calls, list):
            raise HTTPException(status_code=502, detail="invalid provider response")
        public_calls: list[dict[str, Any]] = []
        for call in tool_calls:
            function = call.get("function") if isinstance(call, dict) else None
            if (
                not isinstance(call, dict)
                or call.get("type") != "function"
                or not isinstance(call.get("id"), str)
                or not isinstance(function, dict)
                or not isinstance(function.get("name"), str)
                or not isinstance(function.get("arguments"), str)
            ):
                raise HTTPException(status_code=502, detail="invalid provider response")
            public_calls.append(
                {
                    "id": call["id"],
                    "type": "function",
                    "function": {
                        "name": function["name"],
                        "arguments": function["arguments"],
                    },
                }
            )
        public_message["tool_calls"] = public_calls

    usage = _bounded_usage(payload)
    if usage is None:
        raise HTTPException(status_code=502, detail="invalid provider response")
    prompt_tokens, completion_tokens, _ = usage
    # ``logprobs`` is a forwarded request field, so it has to come back or
    # forwarding it would be a silent no-op -- the caller sets the flag, pays for
    # the larger provider response, and receives nothing. Passed through as the
    # provider sent it; only the two providers serving this model that support
    # logprobs populate it, and the rest return null, which is the honest
    # answer. ``top_logprobs`` is refused at the door, so the per-token
    # alternatives array is never present and the body stays bounded.
    logprobs = choice.get("logprobs")
    if logprobs is not None and not isinstance(logprobs, dict):
        raise HTTPException(status_code=502, detail="invalid provider response")
    return {
        "id": response_id,
        "object": "chat.completion",
        "created": created,
        "model": model,
        "choices": [
            {
                "index": index,
                "finish_reason": finish_reason,
                "message": public_message,
                "logprobs": logprobs,
            }
        ],
        "usage": {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens,
        },
    }


def _provider_preferences(
    *,
    routing_mode: str,
    provider: str,
    quantization: str | None,
) -> dict[str, Any]:
    if routing_mode == "aggregate_throughput":
        return {
            # Normal traffic optimizes for the throughput miners actually feel.
            # CoreWeave is excluded because its reviewed route is 4-bit. A
            # separate bounded recovery phase below deliberately switches
            # objectives after this fast path has failed.
            "sort": "throughput",
            "ignore": ["coreweave"],
            "allow_fallbacks": True,
            "data_collection": "deny",
            "zdr": True,
        }
    preferences: dict[str, Any] = {
        "only": [provider],
        "allow_fallbacks": False,
        "data_collection": "deny",
        "zdr": True,
    }
    if quantization:
        preferences["quantizations"] = [quantization]
    return preferences


def _reliability_provider_preferences() -> dict[str, Any]:
    """Reviewed recovery order after the normal throughput route is exhausted."""
    return {
        "order": ["deepinfra", "groq"],
        "ignore": ["coreweave"],
        "allow_fallbacks": False,
        "data_collection": "deny",
        "zdr": True,
    }


def _openrouter_attempt_count(payload: object) -> int:
    """Extract a bounded count of OpenRouter-internal provider attempts."""
    if not isinstance(payload, dict):
        return 0
    metadata = payload.get("openrouter_metadata")
    if not isinstance(metadata, dict):
        return 0
    attempt = metadata.get("attempt")
    if (
        isinstance(attempt, int)
        and not isinstance(attempt, bool)
        and 1 <= attempt <= 100
    ):
        return attempt
    attempts = metadata.get("attempts")
    if isinstance(attempts, list) and len(attempts) <= 100:
        return len(attempts)
    return 0


def _openrouter_last_attempted_provider(payload: object) -> str | None:
    """Return the last bounded provider named in router attempt metadata."""
    if not isinstance(payload, dict):
        return None
    metadata = payload.get("openrouter_metadata")
    if not isinstance(metadata, dict):
        return None
    attempts = metadata.get("attempts")
    if not isinstance(attempts, list) or not 1 <= len(attempts) <= 100:
        return None
    last = attempts[-1]
    provider = last.get("provider") if isinstance(last, dict) else None
    return provider if isinstance(provider, str) and 1 <= len(provider) <= 120 else None


def _phase_error_code(error: HTTPException) -> str:
    detail = error.detail if isinstance(error.detail, str) else ""
    return {
        "provider response is too large": "response_too_large",
        "invalid provider response": "invalid_provider_response",
        "provider identity mismatch": "provider_identity_mismatch",
        "inference provider unavailable": "provider_unavailable",
    }.get(detail, "provider_unavailable")


async def _complete_chat_with_recovery(
    client: httpx.AsyncClient,
    config: InferenceProxyConfig,
    *,
    payload: dict[str, Any],
    model: str,
    expected_provider: str,
    expected_quantization: str | None,
    expected_prompt_price: float | None,
    expected_completion_price: float | None,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> _ChatCompletionResult:
    """Run fast OpenRouter, then the bounded reliable OpenRouter route.

    Each phase is bounded. The first phase retains throughput routing; the
    second restricts recovery to the reviewed reliable providers. No request
    or response content is retained by this orchestration layer.
    """
    aggregate = config.routing_mode == "aggregate_throughput"
    phases: list[tuple[int, str, dict[str, str], dict[str, Any]]] = []
    primary = dict(payload)
    primary["provider"] = _provider_preferences(
        routing_mode=config.routing_mode,
        provider=expected_provider,
        quantization=expected_quantization,
    )
    phases.append(
        (
            0,
            config.upstream_url,
            _openrouter_headers(config.openrouter_api_key or "", include_metadata=True),
            primary,
        )
    )
    if aggregate:
        reliable = dict(payload)
        reliable["provider"] = _reliability_provider_preferences()
        phases.append(
            (
                1,
                config.upstream_url,
                _openrouter_headers(
                    config.openrouter_api_key or "", include_metadata=True
                ),
                reliable,
            )
        )

    total_attempts = 0
    router_attempts = 0
    last_code = "provider_unavailable"
    last_timed_out = False
    last_phase = 0
    last_provider: str | None = None
    route_observable = False
    for phase, url, headers, phase_payload in phases:
        last_phase = phase
        try:
            provider_result = await _post_provider_with_retry(
                client,
                url,
                payload=phase_payload,
                headers=headers,
                sleep=sleep,
            )
        except _ProviderCallError as error:
            total_attempts += error.attempts
            last_timed_out = error.timed_out
            last_code = "provider_timeout" if error.timed_out else "provider_transport"
            route_observable = True
            continue

        total_attempts += provider_result.attempts
        response = provider_result.response
        raw = response.content
        if len(raw) > config.response_body_bytes:
            last_code = "response_too_large"
            continue
        try:
            decoded = response.json()
        except ValueError:
            decoded = None
        router_attempts += _openrouter_attempt_count(decoded)
        last_provider = _openrouter_last_attempted_provider(decoded) or last_provider
        if response.status_code >= 400:
            last_timed_out = response.status_code in {408, 504}
            last_code = f"upstream_http_{response.status_code}"
            route_observable = (
                route_observable
                or _provider_rejection_is_route_observable(response.status_code)
            )
            continue
        route_observable = True
        if not isinstance(decoded, dict):
            last_code = "invalid_provider_response"
            continue
        if decoded.get("model") != model:
            last_code = "provider_identity_mismatch"
            continue
        try:
            provider_value = _upstream_provider(decoded)
            if provider_value is None or (
                not aggregate and provider_value != expected_provider
            ):
                raise HTTPException(
                    status_code=502, detail="provider identity mismatch"
                )
            bounded = _bounded_usage(decoded)
            if bounded is None:
                raise HTTPException(status_code=502, detail="invalid provider response")
            prompt, completion, _ = bounded
            if aggregate:
                provider_cost = _bounded_provider_cost(decoded)
                if provider_cost is None:
                    raise HTTPException(
                        status_code=502, detail="invalid provider response"
                    )
                cost = provider_cost
            else:
                if expected_prompt_price is None or expected_completion_price is None:
                    raise HTTPException(
                        status_code=502, detail="invalid provider response"
                    )
                cost = int(
                    (
                        prompt * expected_prompt_price
                        + completion * expected_completion_price
                    )
                    * 1_000_000
                )
            public_response = _public_provider_response(decoded)
        except HTTPException as error:
            last_code = _phase_error_code(error)
            continue
        return _ChatCompletionResult(
            raw=json.dumps(public_response, separators=(",", ":")).encode(),
            usage=(prompt, completion, cost),
            upstream_provider=provider_value,
            upstream_attempts=total_attempts,
            openrouter_attempts=router_attempts,
            fallback_phase=phase,
        )

    raise _ChatProviderExhausted(
        upstream_attempts=total_attempts,
        openrouter_attempts=router_attempts,
        fallback_phase=last_phase,
        terminal_error_code=last_code,
        timed_out=last_timed_out,
        upstream_provider=last_provider,
        route_observable=route_observable,
    )


def _openrouter_headers(
    api_key: str, *, include_metadata: bool = False
) -> dict[str, str]:
    """Headers shared by every OpenRouter request made for DittoBench."""
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        # OpenRouter's official app-attribution contract. Keeping these on both
        # chat and embedding calls prevents one workload from disappearing from
        # the public app accounting page.
        "HTTP-Referer": "https://heyditto.ai/",
        "X-OpenRouter-Title": "Ditto",
    }
    if include_metadata:
        # OpenRouter keeps route identity private unless explicitly requested.
        # It is consumed for trusted telemetry and removed from the public
        # response allowlist.
        headers["X-OpenRouter-Metadata"] = "enabled"
    return headers


# How this gate treats an unexpected request field, and why it changed
# ---------------------------------------------------------------------
#
# **The default is FORWARD.** Forwarding needs no justification; every pin and
# every drop below carries a concrete, stated reason. This is a retrieval-agent
# competition, not a prompt-shape competition -- a harness's own sampling and
# observability choices are its business.
#
# The default used to be the opposite, and it was expensive. Anything outside a
# flat allowlist was a 400 raised *before* ``begin_inference_request``, so no
# ``inference_requests`` row was written, the rejections were invisible in
# telemetry, and the error named no key. ``Cooking`` (rank 15 on v1) burned three
# submissions on one line sending ``reasoning: {"effort": "medium"}`` -- the very
# value this platform already stamps on their request (``benchmark_reasoning``)
# -- with no way to discover which field killed the run.
#
# The governing rule for a drop: **stripping a field is only acceptable when it
# changes neither what the model produces nor what the harness can observe.**
# Silently discarding a knob a miner deliberately set is the Cooking bug with
# extra steps -- they ask for a JSON schema, receive prose, their parser fails,
# and nothing anywhere names the cause. Fields with genuinely zero effect on
# either (``user``, ``metadata``) are inert to strip; fields that shape
# generation or output (``response_format``, ``logit_bias``, ``logprobs``) are
# not, and are forwarded.
#
# The four fates:
#
# ``_PINNED_REQUEST_FIELDS``    accepted, then overwritten or removed in
#                               ``_locked_upstream_payload`` so the ticket's own
#                               value governs. This is the anti-cheat boundary
#                               and it does not loosen: nothing here can obtain
#                               compute, a model, or an effort the ticket did
#                               not grant.
# ``_DROPPED_REQUEST_FIELDS``   accepted, then removed. Zero effect on the
#                               completion or on what the harness observes.
# ``_FORWARDED_REQUEST_FIELDS`` validated and passed through unchanged.
# (refused)                     named explicitly in ``_validate_request_schema``
#                               with the reason, including the two capability
#                               limits (``stream``, ``top_logprobs``).
#
# Provider support was established empirically against the pinned v7 model
# rather than from documentation -- see the table in the PR. Two findings drive
# the placements below:
#
#  * OpenRouter EXCLUDES an endpoint that lacks a requested parameter rather
#    than routing to it and failing. Pinning ``order: [amazon-bedrock]`` (which
#    advertises no ``response_format``) returns 404 "No endpoints found" with
#    the field present and succeeds without it. Under the v7 aggregate route
#    (``allow_fallbacks: true``) the request simply lands on one of the eight
#    supporting providers, so forwarding ``response_format`` cannot manufacture
#    a 400.
#  * ``reasoning_effort`` is the one field that HARD-FAILS: OpenRouter answers
#    400 `"reasoning_effort" and "reasoning.effort" are both provided with
#    conflicting values` whenever it disagrees with the pinned block. Forwarding
#    it would break every request that sets it to anything but ``medium``.

# The anti-cheat boundary. Accepted, then replaced or removed so the ticket's
# value governs; see ``_locked_upstream_payload``.
_PINNED_REQUEST_FIELDS = {
    # Substituted by ``_locked_grant_model``: the grant pins the model.
    "model",
    # Clamped down to the ticket ceiling by ``_output_token_limit``.
    "max_tokens",
    "max_completion_tokens",
    # Forced to 1. Extra completions are extra billed generations.
    "n",
    # Buys N server-side generations and bills for all of them.
    "best_of",
    # Replaced with the versioned benchmark reasoning contract. V7/V8 remain
    # pinned to medium. V9 treats low/medium/high as an agent strategy while
    # keeping provider-only controls pinned by the platform.
    "reasoning",
    # The flat sibling. Canonicalized into the nested block for V9 and removed
    # before forwarding so OpenRouter never receives conflicting aliases.
    "reasoning_effort",
    # OpenRouter's legacy boolean for returning reasoning. Removed so the pinned
    # ``exclude: true`` cannot be overridden into echoing reasoning back.
    "include_reasoning",
    # Selects a provider priority/cost tier -- ``priority`` buys faster compute
    # at a higher price. A compute lever the ticket did not grant.
    "service_tier",
    # The platform derives trusted cost from the response's usage block
    # (``_bounded_provider_cost`` under aggregate routing). A caller must not be
    # able to reshape the input to its own metering.
    "usage",
    # Controls provider-side prompt-cache bucketing. Two agents could collide on
    # a bucket, or one could deliberately target another's, which is cross-miner
    # interference rather than a private choice.
    "prompt_cache_key",
}

# Accepted, then stripped. Each has zero effect on the completion AND zero
# effect on what the harness can observe, which is what makes stripping inert
# rather than a silent behaviour change.
_DROPPED_REQUEST_FIELDS = {
    # Caller-controlled strings that travel to a third party under the
    # ``data_collection: "deny"`` / ``zdr: true`` posture this proxy pins, and
    # that identify or fingerprint the agent behind the request. None of them
    # affects the completion, so dropping cannot change a harness's results.
    "user",
    "metadata",
    "safety_identifier",
    # Asks the provider to retain the completion, directly contradicting the
    # pinned retention posture above.
    "store",
    # Only meaningful alongside ``stream: true``, which this lane refuses.
    "stream_options",
}

# Everything else a reasonable OpenAI-compatible harness sends. Validated where
# a malformed value would be a genuine error, then passed through untouched.
_FORWARDED_REQUEST_FIELDS = {
    "messages",
    "tools",
    "tool_choice",
    "parallel_tool_calls",
    # Sampling knobs. The miner's agent design owns these; they cost nothing
    # extra and silently dropping one would change an agent's behaviour behind
    # its back. ``seed`` and ``stop`` were always forwarded; the rest are the
    # same class and were only ever excluded by the allowlist's silence.
    "temperature",
    "top_p",
    "top_k",
    "min_p",
    "top_a",
    "seed",
    "stop",
    "frequency_penalty",
    "presence_penalty",
    "repetition_penalty",
    # Biases token selection. Buys no compute and grants no model. The earlier
    # tokenizer-mismatch worry does not survive contact: the grant pins
    # gpt-oss-20b and the exchange response tells the harness exactly which
    # model it is talking to, so a bias map is built against the served model.
    "logit_bias",
    # Purely observational -- it does not alter generation at all, so there was
    # never a determinism or comparability argument against it, only the
    # allowlist's silence. Plumbed through ``_public_provider_response`` so it
    # is genuinely returned rather than accepted and quietly discarded.
    # (``top_logprobs`` is refused separately: see the response-size limit.)
    "logprobs",
    # Structured outputs. Verified end to end against the pinned v7 model on all
    # five supporting providers tested, WITH the pinned reasoning block active:
    # ``json_schema`` conforms every time. ``json_object`` is honoured by most
    # providers but is advisory -- prefer ``json_schema``.
    "response_format",
    "structured_outputs",
    # Speculative decoding. Rejected prediction tokens bill as completion
    # tokens, but the prediction blob rides in the request body, so it inflates
    # the caller's own byte-derived reservation and chargeable ceiling first.
    # Self-limiting.
    "prediction",
    # Generation-length hint on newer OpenAI-compatible surfaces.
    "verbosity",
    # Refused when true (see below), but must parse as a known field so the
    # refusal can explain itself.
    "stream",
}

_ALLOWED_REQUEST_FIELDS = (
    _PINNED_REQUEST_FIELDS | _DROPPED_REQUEST_FIELDS | _FORWARDED_REQUEST_FIELDS
)


# Fields refused on purpose, each with the reason the caller gets told. A
# refusal is only acceptable when it is legible, so every entry here explains
# itself rather than saying "unsupported".
_REFUSED_REQUEST_FIELDS = {
    # Route identity and model selection. The platform sets provider preferences
    # itself; letting a caller choose is the evasion path this lane exists to
    # close.
    "models": "the model is pinned by the ticket, not chosen by the request",
    "provider": "provider routing is pinned by the platform",
    "route": "provider routing is pinned by the platform",
    "preset": "provider routing is pinned by the platform",
    "transforms": "prompt transforms would change benchmark semantics",
    # Server-side network egress. The harness runs sandboxed without egress and
    # these would hand it some through the provider.
    "plugins": "server-side plugins are not available on this lane",
    "web_search_options": "server-side web search is not available on this lane",
    # Deprecated tool spellings. No provider serving this model advertises them,
    # so forwarding would leave the harness silently toolless -- name the modern
    # equivalent instead.
    "functions": "use tools instead",
    "function_call": "use tool_choice instead",
    # This lane's response contract is text-only (`_public_provider_response`);
    # a non-text modality would surface as a 502 blamed on the provider.
    "audio": "this lane serves text completions only",
    "modalities": "this lane serves text completions only",
    # Capability limit, not a policy choice. `top_logprobs` multiplies the
    # response body by roughly 80 bytes per token per alternative: measured
    # against this model, `logprobs` alone extrapolates to ~0.81 MiB at the 8192
    # -token ceiling and fits, while top_logprobs=3 reaches ~2.65 MiB and
    # top_logprobs=20 ~13.3 MiB, both past the 2 MiB response cap. Breaching it
    # raises a 502 that is attributed to the PROVIDER and cools the shared
    # route, so a caller's own choice would be charged to infrastructure.
    # Refused rather than clamped because no non-zero value is safe at the
    # ceiling. Raising `response_body_bytes` would unlock it.
    "top_logprobs": (
        "logprobs is supported but top_logprobs is not: it would exceed this "
        "lane's response size limit"
    ),
}


def _validate_request_schema(payload: dict[str, Any]) -> None:
    """Accept the OpenAI-compatible surface, pinning only what protects the grant."""
    # Named refusals first, so a caller sending one gets the reason rather than
    # a bare "unsupported parameter".
    refused = sorted(set(payload) & set(_REFUSED_REQUEST_FIELDS))
    if refused:
        reasons = "; ".join(
            f"{key} ({_REFUSED_REQUEST_FIELDS[key]})" for key in refused
        )
        raise HTTPException(
            status_code=400, detail=f"unsupported inference parameter: {reasons}"
        )
    unknown = set(payload) - _ALLOWED_REQUEST_FIELDS
    if unknown:
        # Name the keys. A harness author reading this over stderr has no other
        # way to learn which of their fields was the problem, and the broker now
        # forwards this detail verbatim (dittobench-api ``forwardChatCompletion``).
        raise HTTPException(
            status_code=400,
            detail=f"unsupported inference parameter: {', '.join(sorted(unknown))}",
        )
    # Validation stays deliberately light. Over-validating a forwarded field
    # recreates the problem this change exists to fix: the point is to reject
    # only what is genuinely malformed, not what is merely unfamiliar.
    for name in (
        "temperature",
        "top_p",
        "frequency_penalty",
        "presence_penalty",
        "min_p",
        "top_a",
        "repetition_penalty",
    ):
        value = payload.get(name)
        if value is not None and (
            not isinstance(value, (int, float))
            or isinstance(value, bool)
            or not math.isfinite(value)
        ):
            raise HTTPException(status_code=400, detail=f"invalid {name}")
    for name in ("frequency_penalty", "presence_penalty"):
        penalty = payload.get(name)
        if penalty is not None and not -2 <= penalty <= 2:
            raise HTTPException(status_code=400, detail=f"invalid {name}")
    for name in ("min_p", "top_a"):
        value = payload.get(name)
        if value is not None and not 0 <= value <= 1:
            raise HTTPException(status_code=400, detail=f"invalid {name}")
    repetition_penalty = payload.get("repetition_penalty")
    if repetition_penalty is not None and not 0 < repetition_penalty <= 2:
        raise HTTPException(status_code=400, detail="invalid repetition_penalty")
    top_k = payload.get("top_k")
    if top_k is not None and (
        not isinstance(top_k, int) or isinstance(top_k, bool) or top_k < 0
    ):
        raise HTTPException(status_code=400, detail="invalid top_k")
    if "logprobs" in payload and not isinstance(payload["logprobs"], bool):
        raise HTTPException(status_code=400, detail="invalid logprobs")
    temperature = payload.get("temperature")
    if temperature is not None and not 0 <= temperature <= 2:
        raise HTTPException(status_code=400, detail="invalid temperature")
    top_p = payload.get("top_p")
    if top_p is not None and not 0 <= top_p <= 1:
        raise HTTPException(status_code=400, detail="invalid top_p")
    seed = payload.get("seed")
    if seed is not None and (
        not isinstance(seed, int)
        or isinstance(seed, bool)
        or not -(2**63) <= seed < 2**63
    ):
        raise HTTPException(status_code=400, detail="invalid seed")
    stop = payload.get("stop")
    if stop is not None and not (
        isinstance(stop, str)
        or (
            isinstance(stop, list)
            and 1 <= len(stop) <= 4
            and all(isinstance(item, str) for item in stop)
        )
    ):
        raise HTTPException(status_code=400, detail="invalid stop")
    for name in ("parallel_tool_calls", "stream", "store"):
        if name in payload and not isinstance(payload[name], bool):
            raise HTTPException(status_code=400, detail=f"invalid {name}")
    # One of the few genuine refusals left. ``stream`` is not normalised to
    # false because this lane answers with a single non-streaming JSON body: a
    # caller that asked for SSE and silently received one would fail parsing it,
    # which is a worse and far less legible outcome than being told.
    if payload.get("stream") not in {None, False}:
        raise HTTPException(
            status_code=400,
            detail=(
                "unsupported inference parameter: stream "
                "(this lane answers with a single non-streaming response)"
            ),
        )
    # ``n`` and ``best_of`` are pinned/dropped in ``_locked_upstream_payload``
    # rather than refused: the provider is asked for exactly one completion no
    # matter what arrives here, so an over-ask cannot buy a second generation.
    # A malformed value is still a malformed request and is still named.
    for name in ("n", "best_of"):
        value = payload.get(name)
        if value is not None and (
            not isinstance(value, int) or isinstance(value, bool) or value < 1
        ):
            raise HTTPException(status_code=400, detail=f"invalid {name}")
    tool_choice = payload.get("tool_choice")
    if tool_choice is not None:
        valid_named_choice = (
            isinstance(tool_choice, dict)
            and set(tool_choice) == {"type", "function"}
            and tool_choice.get("type") == "function"
            and isinstance(tool_choice.get("function"), dict)
            and set(tool_choice["function"]) == {"name"}
            and isinstance(tool_choice["function"].get("name"), str)
            and bool(tool_choice["function"]["name"])
        )
        valid_string_choice = isinstance(tool_choice, str) and tool_choice in {
            "none",
            "auto",
            "required",
        }
        if not valid_string_choice and not valid_named_choice:
            raise HTTPException(status_code=400, detail="invalid tool_choice")
    messages = payload.get("messages")
    if not isinstance(messages, list) or not messages:
        raise HTTPException(status_code=400, detail="messages must be non-empty")
    for message in messages:
        if not isinstance(message, dict):
            raise HTTPException(status_code=400, detail="invalid message")
        role = message.get("role")
        if not isinstance(role, str):
            raise HTTPException(status_code=400, detail="invalid message")
        allowed = {
            "system": {"role", "content"},
            "user": {"role", "content"},
            "assistant": {"role", "content", "tool_calls"},
            # ``name`` is an optional legacy/OpenAI-compatible annotation.
            # Some otherwise valid harnesses include it redundantly alongside
            # ``tool_call_id``. Accept it at the caller boundary, validate it
            # below, and remove it from the locked provider payload.
            "tool": {"role", "content", "tool_call_id", "name"},
        }.get(role)
        if allowed is None or set(message) - allowed:
            raise HTTPException(status_code=400, detail="invalid message")
        content = message.get("content")
        text_parts = (
            isinstance(content, list)
            and bool(content)
            and all(
                isinstance(part, dict)
                and set(part) == {"type", "text"}
                and part.get("type") == "text"
                and isinstance(part.get("text"), str)
                for part in content
            )
        )
        if content is not None and not isinstance(content, str) and not text_parts:
            raise HTTPException(status_code=400, detail="text content only")
        if (
            role == "tool"
            and "name" in message
            and (not isinstance(message["name"], str) or not message["name"])
        ):
            raise HTTPException(status_code=400, detail="invalid tool name")
        tool_calls = message.get("tool_calls", [])
        if not isinstance(tool_calls, list):
            raise HTTPException(status_code=400, detail="invalid tool calls")
        for call in tool_calls:
            if not isinstance(call, dict) or set(call) - {"id", "type", "function"}:
                raise HTTPException(status_code=400, detail="invalid tool call")
            function = call.get("function")
            if (
                call.get("type") != "function"
                or not isinstance(call.get("id"), str)
                or not isinstance(function, dict)
                or set(function) - {"name", "arguments"}
                or not isinstance(function.get("name"), str)
                or not isinstance(function.get("arguments"), str)
            ):
                raise HTTPException(status_code=400, detail="invalid tool call")
    tools = payload.get("tools", [])
    if not isinstance(tools, list):
        raise HTTPException(status_code=400, detail="invalid tools")
    for tool in tools:
        if not isinstance(tool, dict) or set(tool) - {"type", "function"}:
            raise HTTPException(status_code=400, detail="function tools only")
        function = tool.get("function")
        if (
            tool.get("type") != "function"
            or not isinstance(function, dict)
            or set(function) - {"name", "description", "parameters", "strict"}
            or not isinstance(function.get("name"), str)
            or not isinstance(function.get("parameters", {}), dict)
        ):
            raise HTTPException(status_code=400, detail="invalid function tool")


def _output_token_limit(payload: dict[str, Any], maximum: int) -> int:
    """Normalize OpenAI's aliases without allowing one to bypass the other.

    Both an over-ask and a disagreement between the two aliases are resolved
    *downward* rather than refused. Asking for more output than the ticket
    grants is the single most likely way for an ordinary OpenAI-compatible
    harness to trip this gate, and killing the run over it buys nothing: the
    clamp already guarantees the ticket's ceiling holds, which is the only
    property that was ever at stake. A caller cannot obtain more output by
    asking for more, by asking twice, or by disagreeing with itself.
    """
    max_tokens_value = payload.get("max_tokens")
    max_completion_tokens = payload.get("max_completion_tokens")
    for name, candidate in (
        ("max_tokens", max_tokens_value),
        ("max_completion_tokens", max_completion_tokens),
    ):
        if candidate is not None and (
            not isinstance(candidate, int) or isinstance(candidate, bool)
        ):
            raise HTTPException(status_code=400, detail=f"invalid {name}")
        # A non-positive ceiling is a malformed request rather than an over-ask,
        # and there is no conservative direction to normalise it in: clamping it
        # up would hand the caller output it did not ask for.
        if candidate is not None and candidate < 1:
            raise HTTPException(status_code=400, detail=f"invalid {name}")
    requested = [
        value
        for value in (max_tokens_value, max_completion_tokens)
        if value is not None
    ]
    # Aliases that disagree resolve to the smaller, so neither can raise the
    # other. Absent both, the ticket ceiling is the default it always was.
    return min(*requested, maximum) if requested else maximum


def _benchmark_reasoning_for_request(
    payload: dict[str, Any], *, model: str, bench_version: int
) -> dict[str, Any] | None:
    """Resolve the provider reasoning block without alias or privacy drift.

    V7/V8 retain their immutable medium contract. Starting with V9, effort is
    an agent-owned strategy: omission defaults to medium, and either OpenAI's
    flat ``reasoning_effort`` alias or OpenRouter's nested
    ``reasoning.effort`` spelling may select low, medium, or high. The provider
    sees only one canonical nested field, while ``exclude`` remains a trusted
    platform privacy control.
    """
    locked = benchmark_reasoning(model)
    if locked is None:
        return None
    if bench_version < 9:
        return locked

    nested_present = "reasoning" in payload
    flat_present = "reasoning_effort" in payload
    nested_effort: str | None = None
    flat_effort: str | None = None

    if nested_present:
        nested = payload["reasoning"]
        # The scorer broker signs its canonical block with exclude=true after
        # stripping the caller's object. Accept that exact trusted shape as
        # well as the caller-facing effort-only shape; every other provider
        # control (including exclude=false) fails closed.
        allowed_shapes = ({"effort"}, {"effort", "exclude"})
        if (
            not isinstance(nested, dict)
            or set(nested) not in allowed_shapes
            or ("exclude" in nested and nested["exclude"] is not True)
        ):
            raise HTTPException(status_code=400, detail="invalid reasoning")
        candidate = nested["effort"]
        if (
            not isinstance(candidate, str)
            or candidate not in BENCH_V9_REASONING_EFFORTS
        ):
            raise HTTPException(status_code=400, detail="invalid reasoning effort")
        nested_effort = candidate

    if flat_present:
        candidate = payload["reasoning_effort"]
        if (
            not isinstance(candidate, str)
            or candidate not in BENCH_V9_REASONING_EFFORTS
        ):
            raise HTTPException(status_code=400, detail="invalid reasoning_effort")
        flat_effort = candidate

    if (
        nested_effort is not None
        and flat_effort is not None
        and nested_effort != flat_effort
    ):
        raise HTTPException(status_code=400, detail="conflicting reasoning effort")

    effort = nested_effort or flat_effort or BENCH_V9_DEFAULT_REASONING_EFFORT
    return {"effort": effort, "exclude": True}


def _locked_grant_model(grant: Any, *, requested: str, config: Any) -> str:
    """Resolve the model from the ticket, not from the caller's request body.

    Benchmark v7 pins exactly one chat model per grant, so the request body is
    advisory at best and adversarial at worst: the miner wrote the harness that
    produced it. Return the grant's model and record any disagreement — an
    agent asking for a model other than the one its ticket pins is a deliberate
    act, not a typo, and is worth surfacing as an evasion signal.

    Pre-v7 grants keep their original semantics exactly: the caller chooses from
    the globally permitted set, so historical replay is unchanged.
    """
    if grant.bench_version < 7:
        if requested not in config.allowed_models:
            raise HTTPException(status_code=403, detail="model is not permitted")
        return requested
    if not grant.allowed_models:
        raise HTTPException(status_code=409, detail="inference route unavailable")
    locked = grant.allowed_models[0]
    if requested != locked:
        logger.warning(
            "v7 inference model mismatch: grant=%s agent=%s slot=%s "
            "requested=%r locked=%r — serving the locked model",
            grant.grant_id,
            grant.agent_id,
            grant.slot_id,
            requested,
            locked,
        )
    return locked


def _locked_upstream_payload(
    payload: dict[str, Any], *, model: str, max_tokens: int, bench_version: int = 7
) -> dict[str, Any]:
    """Force consensus model/reasoning fields before provider routing.

    This is the half of the schema policy that makes permissiveness safe. Every
    field the gate now *accepts* rather than refuses is either replaced here
    with the ticket's own value or removed here before the request leaves the
    process, so accepting it cannot buy the caller compute, a model, or a
    reasoning effort its ticket did not grant.
    """
    upstream = dict(payload)
    # Accepted at the door purely so the caller is not killed for sending them;
    # none of them reaches the provider. See ``_DROPPED_REQUEST_FIELDS``.
    for field in _DROPPED_REQUEST_FIELDS:
        upstream.pop(field, None)
    # Pinned by removal: the ticket's value governs, and for these that value is
    # "whatever the platform's own request says", so the caller's key must go.
    # ``reasoning_effort`` in particular is not merely ignored upstream -- it
    # hard-400s when it disagrees with the pinned ``reasoning.effort``.
    for field in (
        "best_of",
        "reasoning_effort",
        "include_reasoning",
        "service_tier",
        "usage",
        "prompt_cache_key",
        # Collapsed into the single clamped ``max_tokens`` computed by
        # ``_output_token_limit``; forwarding both re-opens the alias bypass.
        "max_completion_tokens",
    ):
        upstream.pop(field, None)
    upstream["model"] = model
    # A tool result is unambiguously correlated by ``tool_call_id``. Some
    # OpenAI-compatible clients also send the legacy/redundant ``name`` field,
    # while not every upstream accepts it. Keep the gateway compatible without
    # changing model-visible content by removing only that annotation.
    messages = upstream.get("messages")
    if isinstance(messages, list):
        upstream["messages"] = [
            {key: value for key, value in message.items() if key != "name"}
            if isinstance(message, dict) and message.get("role") == "tool"
            else message
            for message in messages
        ]
    upstream["max_tokens"] = max_tokens
    upstream["n"] = 1
    upstream["stream"] = False
    # Unconditional assignment, not a merge: provider-only nested controls
    # never survive. V7/V8 use the historical fixed-medium contract; V9 uses
    # the validated agent effort (or medium when omitted).
    reasoning = _benchmark_reasoning_for_request(
        payload, model=model, bench_version=bench_version
    )
    if reasoning is None:
        upstream.pop("reasoning", None)
    else:
        upstream["reasoning"] = reasoning
    return upstream


async def _resolve_admission_config(
    request: Request, config: InferenceProxyConfig
) -> InferenceProxyConfig:
    """``config`` with the operator's live admission policy overlaid.

    Note that the overlaid ``request_budget`` is inert on this path: admission
    compares against ``grant.request_budget``, the value stamped on the grant
    row when the lease was minted. That is deliberate -- see
    ``api_models/inference_concurrency_settings.py``.
    """
    return await resolved_proxy_config(request.app.state, config)


def _provider_rejection_is_route_observable(status_code: int) -> bool:
    """Exclude caller-shape failures from shared provider-route health."""
    return status_code >= 400 and status_code not in {400, 422}


def _validated_embedding_payload(
    payload: Any, *, model: str, dimensions: int
) -> list[str]:
    if not isinstance(payload, dict) or set(payload) != {
        "model",
        "input",
        "dimensions",
        "encoding_format",
    }:
        raise HTTPException(status_code=400, detail="invalid embedding request")
    inputs = payload.get("input")
    # The embedding model is pinned by the ticket and substituted by the trusted
    # broker before the request ever reaches here, so a disagreement is not a
    # harness configuration choice — it means something upstream of this
    # boundary tried to select a different embedding space. Surface it as an
    # evasion signal, then fail closed. (Unlike chat, this stays a rejection:
    # silently rewriting the model would change the vector space under a caller
    # that is validating dimensions, which is strictly worse than refusing.)
    requested_model = payload.get("model")
    if requested_model != model:
        logger.warning(
            "v7 embedding model mismatch: requested=%r locked=%r — refusing",
            requested_model,
            model,
        )
    if (
        requested_model != model
        or payload.get("dimensions") != dimensions
        or payload.get("encoding_format") != "float"
        or not isinstance(inputs, list)
        or not 1 <= len(inputs) <= _EMBEDDING_MAX_INPUTS
        or any(not isinstance(value, str) or not value for value in inputs)
    ):
        raise HTTPException(status_code=400, detail="invalid embedding request")
    return inputs


def _public_embedding_response(
    payload: Any, *, model: str, dimensions: int, input_count: int
) -> tuple[dict[str, Any], int]:
    response_models = {model}
    if model == _PPLX_EMBED_CONTRACT_MODEL:
        # OpenRouter accepts the catalog-qualified model ID but Perplexity's
        # response canonicalizes that exact reviewed model to its unqualified
        # name. Keep the outbound contract frozen while accepting only this
        # observed response alias.
        response_models.add(_PPLX_EMBED_RESPONSE_MODEL)
    if not isinstance(payload, dict) or payload.get("model") not in response_models:
        raise HTTPException(status_code=502, detail="provider identity mismatch")
    data = payload.get("data")
    usage = payload.get("usage")
    prompt_tokens = usage.get("prompt_tokens") if isinstance(usage, dict) else None
    if (
        not isinstance(data, list)
        or len(data) != input_count
        or not isinstance(prompt_tokens, int)
        or isinstance(prompt_tokens, bool)
        or prompt_tokens < 0
    ):
        raise HTTPException(status_code=502, detail="invalid provider response")
    # The vector payload is forwarded as parsed, without inspecting elements.
    #
    # Envelope shape, ordering, arity, and length are still checked below --
    # those are O(vectors) and catch a truncated or misaligned provider
    # response. What is deliberately NOT done is per-element float validation.
    # At 768 dimensions that cost 42us per vector (measured), and a v7 run
    # issues ~671 embedding calls of up to 256 inputs each: 0.2s of blocking
    # CPU per run at small batches and 7.2s at the maximum, all of it on the
    # single event loop that also ingests validator heartbeats. It was roughly
    # a 50% surcharge on top of the unavoidable json.loads of the same body.
    #
    # It also bought nothing. This proxy is not the consumer of these vectors;
    # the trusted broker is, and it independently re-validates every element
    # for NaN/Inf plus each vector's length and index before any harness sees
    # them (dittobench-api cmd/dittobench-api/inference_broker.go:1144-1151).
    # This was the second copy of a check the actual consumer performs anyway.
    public_data: list[dict[str, Any]] = []
    for expected_index, item in enumerate(data):
        vector = item.get("embedding") if isinstance(item, dict) else None
        if (
            not isinstance(item, dict)
            or item.get("index") != expected_index
            or not isinstance(vector, list)
            or len(vector) != dimensions
        ):
            raise HTTPException(status_code=502, detail="invalid provider response")
        public_data.append(
            {
                "object": "embedding",
                "index": expected_index,
                "embedding": vector,
            }
        )
    return (
        {
            "object": "list",
            "model": model,
            "data": public_data,
            "usage": {
                "prompt_tokens": prompt_tokens,
                "total_tokens": prompt_tokens,
            },
        },
        prompt_tokens,
    )


@router.post("/exchange", response_model=InferenceExchangeResponse)
async def exchange_inference_grant(
    payload: InferenceExchangeRequest,
    request: Request,
    response: Response,
    chain: ChainDep,
    session: SessionDep,
    x_validator_hotkey: Annotated[str | None, Header()] = None,
) -> InferenceExchangeResponse:
    """Rotate a ticket grant onto one validator-authorized trusted broker key."""
    config = request.app.state.config.inference_proxy
    if not config.enabled:
        raise HTTPException(status_code=404, detail="inference proxy is disabled")
    if x_validator_hotkey != payload.validator_hotkey:
        raise ValidatorAuthError("inference exchange hotkey mismatch")
    if (
        abs(datetime.now(UTC) - payload.requested_at.astimezone(UTC))
        > _EXCHANGE_MAX_AGE
    ):
        raise HTTPException(status_code=409, detail="inference exchange is stale")
    if not _verify_signature(
        payload.validator_hotkey, _exchange_message(payload), payload.signature
    ):
        raise ValidatorAuthError("inference exchange signature did not verify")
    await _assert_validator_permitted(
        chain,
        request.app.state.config.chain.netuid,
        payload.validator_hotkey,
        network=request.app.state.config.chain.subtensor_network,
    )
    now = datetime.now(UTC)
    async with session.begin():
        try:
            await consume_validator_nonce(
                session,
                nonce=payload.nonce,
                validator_hotkey=payload.validator_hotkey,
                now=now,
                expires_at=now + _EXCHANGE_MAX_AGE,
            )
        except ValidatorRequestReplayError as error:
            raise HTTPException(
                status_code=409, detail="inference exchange nonce was already used"
            ) from error
        activated = await activate_inference_grant(
            session,
            grant_id=payload.grant_id,
            validator_hotkey=payload.validator_hotkey,
            broker_public_key=payload.broker_public_key,
            now=now,
            config=config,
        )
    if activated is None:
        raise HTTPException(status_code=409, detail="inference grant is not live")
    grant, bearer = activated
    response.headers["Cache-Control"] = "no-store"
    return InferenceExchangeResponse(
        grant_id=grant.grant_id,
        bearer=bearer,
        proxy_url=f"{config.public_base_url}/api/v1/inference/chat/completions",
        expires_at=grant.expires_at,
        generation=grant.generation,
        provider=grant.route_provider if grant.bench_version >= 7 else None,
        profile_revision=grant.route_profile if grant.bench_version >= 7 else None,
        model=grant.allowed_models[0] if grant.bench_version >= 7 else None,
    )


@router.post("/chat/completions")
async def proxy_chat_completions(
    request: Request,
    x_ditto_grant: Annotated[UUID | None, Header()] = None,
    x_ditto_generation: Annotated[int | None, Header()] = None,
    x_ditto_nonce: Annotated[UUID | None, Header()] = None,
    x_ditto_requested_at: Annotated[datetime | None, Header()] = None,
    x_ditto_proof: Annotated[str | None, Header()] = None,
    authorization: Annotated[str | None, Header()] = None,
) -> Response:
    """Proxy one non-streaming chat request under atomic ticket budgets."""
    config = request.app.state.config.inference_proxy
    if not config.enabled or config.openrouter_api_key is None:
        raise HTTPException(status_code=404, detail="inference proxy is disabled")
    if None in {
        x_ditto_grant,
        x_ditto_generation,
        x_ditto_nonce,
        x_ditto_requested_at,
        x_ditto_proof,
        authorization,
    }:
        raise HTTPException(status_code=401, detail="missing inference proof")
    assert x_ditto_grant is not None
    assert x_ditto_generation is not None
    assert x_ditto_nonce is not None
    assert x_ditto_requested_at is not None
    assert x_ditto_proof is not None
    assert authorization is not None
    if not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="invalid inference proof")
    body = await request.body()
    if len(body) > config.request_body_bytes:
        raise HTTPException(status_code=413, detail="inference request is too large")
    if abs(datetime.now(UTC) - x_ditto_requested_at.astimezone(UTC)) > _PROXY_MAX_AGE:
        raise HTTPException(status_code=409, detail="inference request is stale")
    try:
        payload = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError) as error:
        raise HTTPException(status_code=400, detail="invalid JSON request") from error
    if not isinstance(payload, dict):
        raise HTTPException(
            status_code=400, detail="inference request must be a JSON object"
        )
    # Every field-level decision, including the streaming refusal, lives in one
    # function now. It used to be split across here and the schema check, which
    # is how the two most common refusals ended up with two different wordings.
    _validate_request_schema(payload)
    requested_model = payload.get("model")
    if not isinstance(requested_model, str):
        raise HTTPException(status_code=403, detail="model is not permitted")
    max_tokens = _output_token_limit(payload, config.max_output_tokens)

    session_maker = request.app.state.session_maker
    now = datetime.now(UTC)
    async with session_maker() as session, session.begin():
        from ditto.db.models import InferenceGrant

        grant = await session.get(InferenceGrant, x_ditto_grant)
        if (
            grant is None
            or grant.broker_public_key is None
            or grant.generation != x_ditto_generation
        ):
            raise HTTPException(status_code=401, detail="invalid inference proof")
        try:
            proof = base64.urlsafe_b64decode(
                x_ditto_proof + "=" * (-len(x_ditto_proof) % 4)
            )
            _decode_public_key(grant.broker_public_key).verify(
                proof,
                _proxy_message(
                    grant_id=x_ditto_grant,
                    generation=x_ditto_generation,
                    nonce=x_ditto_nonce,
                    requested_at=x_ditto_requested_at,
                    body=body,
                ),
            )
        except (ValueError, InvalidSignature) as error:
            raise HTTPException(
                status_code=401, detail="invalid inference proof"
            ) from error
        # The model is a property of the ticket, never of the request. A miner
        # controls every byte of the harness that produced this body, so the
        # only trustworthy source is the grant the platform itself minted. For
        # v7 the grant pins exactly one model; take it and discard whatever the
        # body asked for, so an agent cannot select a cheaper or stronger model
        # by editing its own code. Metering below uses the same locked value.
        model = _locked_grant_model(grant, requested=requested_model, config=config)
        # V9 reasoning is an agent strategy, but it is still validated before
        # reserving provider capacity or writing request accounting. Invalid or
        # conflicting aliases therefore fail without spending the grant.
        _benchmark_reasoning_for_request(
            payload, model=model, bench_version=grant.bench_version
        )
        reserved = await begin_inference_request(
            session,
            grant_id=x_ditto_grant,
            nonce=x_ditto_nonce,
            bearer=authorization.removeprefix("Bearer "),
            model=model,
            # Reserve a realistic estimate of what this call will cost, plus the
            # output the caller is permitted, so concurrent requests cannot
            # collectively cross the ticket budget. The byte-length ceiling
            # travels alongside it for the provider-accounting bound; see
            # ``_estimated_tokens`` for why these are two numbers.
            token_reservation=max_tokens + _estimated_tokens(body),
            max_chargeable_tokens=_max_chargeable_tokens(
                body, output_tokens=max_tokens
            ),
            now=now,
            config=config,
        )
        # Every refusal now arrives as a named decline; there is no longer a
        # bare ``None`` meaning "refused, no comment". The one that used to hide
        # here was a spent token budget, which reached the broker as 4100 and
        # was retried until the run died with hours left on the lease.
        if isinstance(reserved, InferenceDecline):
            raise InferenceDeclinedError(reserved, lane="inference")

    reserved_grant = reserved[0]
    upstream_payload = _locked_upstream_payload(
        payload,
        model=model,
        max_tokens=max_tokens,
        bench_version=reserved_grant.bench_version,
    )
    if not reserved_grant.route_provider:
        raise HTTPException(status_code=409, detail="inference route unavailable")
    status = "failed"
    usage: tuple[int, int, int] | None = None
    raw: bytes | None = None
    started = time.monotonic()
    timed_out = False
    route_observable = False
    upstream_provider: str | None = None
    upstream_attempts = 0
    openrouter_attempts = 0
    fallback_phase = 0
    terminal_error_code: str | None = None
    try:
        recovered = await _complete_chat_with_recovery(
            request.app.state.inference_client,
            config,
            payload=upstream_payload,
            model=model,
            expected_provider=reserved_grant.route_provider,
            expected_quantization=reserved_grant.route_quantization,
            expected_prompt_price=reserved_grant.route_prompt_price_per_token,
            expected_completion_price=(reserved_grant.route_completion_price_per_token),
        )
        raw = recovered.raw
        usage = recovered.usage
        upstream_provider = recovered.upstream_provider
        upstream_attempts = recovered.upstream_attempts
        openrouter_attempts = recovered.openrouter_attempts
        fallback_phase = recovered.fallback_phase
        route_observable = True
        status = "completed"
    except _ChatProviderExhausted as error:
        upstream_attempts = error.upstream_attempts
        openrouter_attempts = error.openrouter_attempts
        fallback_phase = error.fallback_phase
        terminal_error_code = error.terminal_error_code
        timed_out = error.timed_out
        upstream_provider = error.upstream_provider
        route_observable = error.route_observable
        detail = (
            "inference provider timed out"
            if timed_out
            else "inference provider unavailable"
        )
        raise HTTPException(
            status_code=504 if timed_out else 502, detail=detail
        ) from error
    finally:
        finished_at = datetime.now(UTC)
        async with session_maker() as session, session.begin():
            deliverable = await finish_inference_request(
                session,
                grant_id=x_ditto_grant,
                nonce=x_ditto_nonce,
                generation=x_ditto_generation,
                status=status,
                prompt_tokens=usage[0] if usage is not None else 0,
                completion_tokens=usage[1] if usage is not None else 0,
                cost_microusd=usage[2] if usage is not None else 0,
                usage_available=usage is not None,
                now=finished_at,
                upstream_provider=upstream_provider,
                timed_out=timed_out,
                latency_ms=max(0, round((time.monotonic() - started) * 1000)),
                upstream_attempts=upstream_attempts,
                openrouter_attempts=openrouter_attempts,
                fallback_phase=fallback_phase,
                terminal_error_code=terminal_error_code,
            )
            from ditto.db.models import InferenceGrant

            observed_grant = await session.get(InferenceGrant, x_ditto_grant)
            if observed_grant is not None and route_observable:
                await record_route_observation(
                    session,
                    grant=observed_grant,
                    success=status == "completed" and usage is not None,
                    latency_ms=(time.monotonic() - started) * 1000,
                    completion_tokens=usage[1] if usage is not None else 0,
                    cost_microusd=usage[2] if usage is not None else 0,
                    timed_out=timed_out,
                    now=finished_at,
                )
    if not deliverable or raw is None:
        raise HTTPException(status_code=409, detail="inference grant is no longer live")
    return Response(
        content=raw,
        status_code=200,
        media_type="application/json",
        headers={"Cache-Control": "no-store"},
    )


@router.post("/embeddings")
async def proxy_embeddings(
    request: Request,
    x_ditto_grant: Annotated[UUID | None, Header()] = None,
    x_ditto_generation: Annotated[int | None, Header()] = None,
    x_ditto_nonce: Annotated[UUID | None, Header()] = None,
    x_ditto_requested_at: Annotated[datetime | None, Header()] = None,
    x_ditto_proof: Annotated[str | None, Header()] = None,
    authorization: Annotated[str | None, Header()] = None,
) -> Response:
    """Proxy the reviewed v7-and-later embedding contract under separate budgets."""
    config = request.app.state.config.inference_proxy
    if not config.enabled or config.openrouter_api_key is None:
        raise HTTPException(status_code=404, detail="inference proxy is disabled")
    if None in {
        x_ditto_grant,
        x_ditto_generation,
        x_ditto_nonce,
        x_ditto_requested_at,
        x_ditto_proof,
        authorization,
    }:
        raise HTTPException(status_code=401, detail="missing inference proof")
    assert x_ditto_grant is not None
    assert x_ditto_generation is not None
    assert x_ditto_nonce is not None
    assert x_ditto_requested_at is not None
    assert x_ditto_proof is not None
    assert authorization is not None
    if not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="invalid inference proof")
    body = await request.body()
    if len(body) > config.embedding_request_body_bytes:
        raise HTTPException(status_code=413, detail="embedding request is too large")
    if abs(datetime.now(UTC) - x_ditto_requested_at.astimezone(UTC)) > _PROXY_MAX_AGE:
        raise HTTPException(status_code=409, detail="embedding request is stale")
    try:
        payload = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError) as error:
        raise HTTPException(status_code=400, detail="invalid JSON request") from error
    inputs = _validated_embedding_payload(
        payload, model=config.embedding_model, dimensions=config.embedding_dimensions
    )

    session_maker = request.app.state.session_maker
    # Resolve the operator's concurrency board BEFORE opening the admission
    # transaction. That transaction holds two FOR UPDATE row locks (the grant
    # and its ticket), so a settings SELECT inside it would lengthen the most
    # contended critical section in the service. The resolver is TTL-cached and
    # reads on its own session, so the common case costs nothing.
    admission_config = await _resolve_admission_config(request, config)
    now = datetime.now(UTC)
    async with session_maker() as session, session.begin():
        from ditto.db.models import InferenceGrant

        grant = await session.get(InferenceGrant, x_ditto_grant)
        if (
            grant is None
            or not _uses_hosted_embeddings(grant.bench_version)
            or grant.broker_public_key is None
            or grant.generation != x_ditto_generation
            or grant.embedding_model != config.embedding_model
            or grant.embedding_profile != config.embedding_profile
            or grant.embedding_provider != config.embedding_provider
            or grant.embedding_dimensions != config.embedding_dimensions
        ):
            raise HTTPException(status_code=401, detail="invalid inference proof")
        try:
            proof = base64.urlsafe_b64decode(
                x_ditto_proof + "=" * (-len(x_ditto_proof) % 4)
            )
            _decode_public_key(grant.broker_public_key).verify(
                proof,
                _proxy_message(
                    grant_id=x_ditto_grant,
                    generation=x_ditto_generation,
                    nonce=x_ditto_nonce,
                    requested_at=x_ditto_requested_at,
                    body=body,
                ),
            )
        except (ValueError, InvalidSignature) as error:
            raise HTTPException(
                status_code=401, detail="invalid inference proof"
            ) from error
        reserved = await begin_inference_request(
            session,
            grant_id=x_ditto_grant,
            nonce=x_ditto_nonce,
            bearer=authorization.removeprefix("Bearer "),
            model=config.embedding_model,
            token_reservation=_estimated_tokens(body),
            max_chargeable_tokens=_max_chargeable_tokens(body),
            now=now,
            config=admission_config,
            request_kind="embedding",
        )
        # AT_CAPACITY still answers 503 + Retry-After exactly as it has since
        # dittobench-api #103 -- the lease is fine and the lane is momentarily
        # full, and 503 is already in the broker's retryable-transient class, so
        # a fleet that has not learned about backpressure still backs off and
        # survives. Every other refusal now carries its own code as well.
        if isinstance(reserved, InferenceDecline):
            raise InferenceDeclinedError(reserved, lane="embedding")

    upstream_payload = {
        "model": config.embedding_model,
        "input": inputs,
        "dimensions": config.embedding_dimensions,
        "encoding_format": "float",
        "provider": {
            "order": [config.embedding_provider],
            "allow_fallbacks": False,
            "data_collection": "deny",
        },
    }
    status = "failed"
    prompt_tokens = 0
    raw: bytes | None = None
    timed_out = False
    upstream_attempts = 0
    started = time.monotonic()
    try:
        provider_result = await _post_provider_with_retry(
            request.app.state.inference_client,
            config.embedding_upstream_url,
            payload=upstream_payload,
            headers=_openrouter_headers(config.openrouter_api_key),
        )
        upstream = provider_result.response
        upstream_attempts = provider_result.attempts
        if len(upstream.content) > config.embedding_response_body_bytes:
            raise HTTPException(
                status_code=502, detail="embedding response is too large"
            )
        if upstream.status_code >= 400:
            raise HTTPException(
                status_code=502, detail="embedding provider unavailable"
            )
        try:
            decoded = upstream.json()
        except ValueError as error:
            raise HTTPException(
                status_code=502, detail="invalid provider response"
            ) from error
        public_response, prompt_tokens = _public_embedding_response(
            decoded,
            model=config.embedding_model,
            dimensions=config.embedding_dimensions,
            input_count=len(inputs),
        )
        raw = json.dumps(public_response, separators=(",", ":")).encode()
        status = "completed"
    except _ProviderCallError as error:
        upstream_attempts = error.attempts
        timed_out = error.timed_out
        detail = (
            "embedding provider timed out"
            if timed_out
            else "embedding provider unavailable"
        )
        raise HTTPException(
            status_code=504 if timed_out else 502, detail=detail
        ) from error
    finally:
        finished_at = datetime.now(UTC)
        async with session_maker() as session, session.begin():
            deliverable = await finish_inference_request(
                session,
                grant_id=x_ditto_grant,
                nonce=x_ditto_nonce,
                generation=x_ditto_generation,
                status=status,
                prompt_tokens=prompt_tokens,
                completion_tokens=0,
                # Catalog price is $0.004 / 1M input tokens. Provider-reported
                # direct cost is intentionally not trusted.
                cost_microusd=round(prompt_tokens * 0.004),
                usage_available=status == "completed",
                now=finished_at,
                upstream_provider=config.embedding_provider,
                timed_out=timed_out,
                latency_ms=max(0, round((time.monotonic() - started) * 1000)),
                upstream_attempts=upstream_attempts,
            )
    if not deliverable or raw is None:
        raise HTTPException(status_code=409, detail="embedding grant is no longer live")
    return Response(
        content=raw,
        status_code=200,
        media_type="application/json",
        headers={"Cache-Control": "no-store"},
    )


__all__ = ["router"]
