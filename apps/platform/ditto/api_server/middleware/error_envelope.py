"""Uniform JSON error envelope for uncaught exceptions."""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.requests import Request

from ditto.api_server.endpoints.inference import InferenceDeclinedError
from ditto.api_server.endpoints.miner_logs import HarnessLogsDeniedError
from ditto.api_server.endpoints.retrieval import (
    AgentNotFoundError,
    HotkeyAgentNotFoundError,
)
from ditto.api_server.endpoints.screener import (
    AgentNotScreenableError,
    ScreenerAuthError,
)
from ditto.api_server.endpoints.validator import (
    AgentNotEvaluatableError,
    RetiredBenchVersionError,
    ValidatorAuthError,
)
from ditto.api_server.middleware.request_id import (
    REQUEST_ID_HEADER,
    request_id_var,
)
from ditto.api_server.payment_verifier import (
    PaymentAmountMismatch,
    PaymentCallTypeMismatch,
    PaymentDestinationMismatch,
    PaymentExtrinsicFailed,
    PaymentNotFoundOnChain,
    PaymentRecoveryExpired,
    PaymentReplayedError,
    PaymentSignerMismatch,
    PaymentVerifierError,
)
from ditto.api_server.pricing import (
    MalformedPriceError,
    OracleUnreachableError,
    PriceTooStaleError,
    PricingError,
)
from ditto.db.queries.agents import SubmissionCooldownError
from ditto.db.queries.inference import InferenceDecline

logger = logging.getLogger(__name__)

# Agent-side error codes (1xxx range per CODE-REVIEW-CHECKLIST). Both
# retrieval not-found codes map to HTTP 404 with distinct envelope
# codes so callers can branch on the specific failure mode.
ERROR_CODE_AGENT_NOT_FOUND = 1200
ERROR_CODE_HOTKEY_AGENT_NOT_FOUND = 1201
ERROR_CODE_HARNESS_LOGS_DENIED = 1202
ERROR_CODE_SUBMISSION_COOLDOWN = 1105

# Platform error codes (3xxx range per CODE-REVIEW-CHECKLIST).
ERROR_CODE_UNHANDLED = 3000
ERROR_CODE_VALIDATION = 3001
ERROR_CODE_HTTP_EXCEPTION = 3002
ERROR_CODE_PRICING = 3100
ERROR_CODE_ORACLE_UNREACHABLE = 3101
ERROR_CODE_MALFORMED_PRICE = 3102
ERROR_CODE_PRICE_TOO_STALE = 3103
ERROR_CODE_PAYMENT_VERIFIER = 3200
ERROR_CODE_PAYMENT_NOT_FOUND = 3201
ERROR_CODE_PAYMENT_EXTRINSIC_FAILED = 3202
ERROR_CODE_PAYMENT_AMOUNT_MISMATCH = 3203
ERROR_CODE_PAYMENT_DESTINATION_MISMATCH = 3204
ERROR_CODE_PAYMENT_SIGNER_MISMATCH = 3205
ERROR_CODE_PAYMENT_CALL_TYPE_MISMATCH = 3206
ERROR_CODE_PAYMENT_REPLAYED = 3207
ERROR_CODE_PAYMENT_RECOVERY_EXPIRED = 3208

# Validator-side error codes (4xxx range). Surfaced to the validator daemon
# driving the evaluate -> score loop; distinct from the agent-side 1xxx codes.
ERROR_CODE_VALIDATOR_AUTH = 4000
ERROR_CODE_AGENT_NOT_EVALUATABLE = 4001
# 4002 -> 410 Gone. TERMINAL. The benchmark era this score belongs to is
# retired; the score ledger's CHECK constraints refuse the row and no future
# event brings the era back. Distinct from 4001, which is about the *agent*
# and can clear when the agent advances -- this one never clears.
#
# 410 rather than 409 on purpose: a conflict invites a retry, and this must
# not be retried. Specifically it must not be handed back as
# ``fail_job(reason="infrastructure")``, which is no-fault and would re-lease
# without bound. See ``RetiredBenchVersionError``.
ERROR_CODE_BENCH_VERSION_RETIRED = 4002

# Inference-proxy declines (41xx, inside the validator range because the caller
# is the validator's broker). These exist because the HTTP status alone cannot
# carry this distinction: 429 has meant three different things on this path, and
# a broker that can only see the status has no way to tell "your lease is dead"
# from "come back in a second". The status stays a coarse hint -- retryable or
# not -- and *these codes are the authoritative signal*.
#
# Contract, in the order a broker should check it:
#   4101 GRANT_REVOKED     -> 429. Fatal. Do not retry; the run is over.
#   4102 BUDGET_EXHAUSTED  -> 429. Terminal but healthy: the lease is alive and
#                             the request-count allowance is spent. Do not
#                             retry, but the run can still wind down and submit
#                             what it has.
#   4103 AT_CAPACITY       -> 503 + Retry-After. Nothing is wrong. Back off and
#                             retry; this must never end a run.
#   4104 TOKEN_BUDGET_EXHAUSTED -> 429. As 4102, but the *token* allowance. Split
#                             out because the two are tuned by different
#                             operator dials, and because this is the one that
#                             actually ends heavy v7 runs.
#   4105 LEASE_EXPIRED     -> 429. The grant's own clock ran out.
#   4106 NONCE_REPLAYED    -> 429. This (grant, nonce) already exists. Retrying
#                             it can never succeed; mint a fresh nonce.
#   4107 MODEL_NOT_PERMITTED -> 429. The grant does not pin this model.
#   4108 GRANT_NOT_EXCHANGED -> 429. Minted but never exchanged for a bearer.
#   4109 RESERVATION_TOO_LARGE -> 429. The request alone exceeds the whole token
#                             allowance; it would not fit in a fresh grant
#                             either. Distinct from 4104 because nothing has
#                             been spent and the lease is still good.
#   4100 unattributed      -> 429. The deliberate remainder: an unknown grant id
#                             or a failed bearer comparison, which are held
#                             indistinguishable on purpose so an unauthenticated
#                             caller learns nothing about someone else's lease.
#                             Treat as fatal.
#
# 4104-4108 are additive. Old brokers ignore the body entirely and see only the
# status, and every one of these is a 429 -- the status old brokers already
# treat as fatal -- so adding them changes nothing a pinned build can observe.
# That asymmetry is the whole reason the retryable case (4103) had to move off
# 429 and onto 503 while these could stay: adding codes is backward-compatible,
# changing statuses is not. See the module docstring in
# `endpoints/inference.py` for why 503 specifically is the safe retryable status.
ERROR_CODE_INFERENCE_DECLINED = 4100
ERROR_CODE_INFERENCE_GRANT_REVOKED = 4101
ERROR_CODE_INFERENCE_BUDGET_EXHAUSTED = 4102
ERROR_CODE_INFERENCE_AT_CAPACITY = 4103
ERROR_CODE_INFERENCE_TOKEN_BUDGET_EXHAUSTED = 4104
ERROR_CODE_INFERENCE_LEASE_EXPIRED = 4105
ERROR_CODE_INFERENCE_NONCE_REPLAYED = 4106
ERROR_CODE_INFERENCE_MODEL_NOT_PERMITTED = 4107
ERROR_CODE_INFERENCE_GRANT_NOT_EXCHANGED = 4108
ERROR_CODE_INFERENCE_RESERVATION_TOO_LARGE = 4109

# Screener-side error codes (5xxx range). Surfaced to the screener worker
# driving the uploaded -> evaluating promotion gate.
ERROR_CODE_SCREENER_AUTH = 5000
ERROR_CODE_AGENT_NOT_SCREENABLE = 5001


# Every terminal decline, in one table rather than a ladder of ifs. A ladder
# invites the failure this whole area keeps having: a new decline is added to
# the enum, nobody adds a branch, and it silently falls through to the anonymous
# 4100 -- which is exactly how a spent token budget spent months looking like an
# unexplained refusal. ``test_every_decline_has_a_distinct_code`` walks the enum
# against this table so a member with no entry fails the suite instead of
# failing a run.
_TERMINAL_DECLINE_RESPONSES: dict[InferenceDecline, tuple[int, str]] = {
    InferenceDecline.GRANT_REVOKED: (
        ERROR_CODE_INFERENCE_GRANT_REVOKED,
        "grant was revoked",
    ),
    InferenceDecline.BUDGET_EXHAUSTED: (
        ERROR_CODE_INFERENCE_BUDGET_EXHAUSTED,
        "grant has spent its request budget",
    ),
    InferenceDecline.TOKEN_BUDGET_EXHAUSTED: (
        ERROR_CODE_INFERENCE_TOKEN_BUDGET_EXHAUSTED,
        "grant has spent its token budget",
    ),
    InferenceDecline.LEASE_EXPIRED: (
        ERROR_CODE_INFERENCE_LEASE_EXPIRED,
        "grant has expired",
    ),
    InferenceDecline.NONCE_REPLAYED: (
        ERROR_CODE_INFERENCE_NONCE_REPLAYED,
        "request nonce was already used",
    ),
    InferenceDecline.MODEL_NOT_PERMITTED: (
        ERROR_CODE_INFERENCE_MODEL_NOT_PERMITTED,
        "grant does not permit this model",
    ),
    InferenceDecline.GRANT_NOT_EXCHANGED: (
        ERROR_CODE_INFERENCE_GRANT_NOT_EXCHANGED,
        "grant has not been exchanged for a bearer",
    ),
    InferenceDecline.RESERVATION_TOO_LARGE: (
        ERROR_CODE_INFERENCE_RESERVATION_TOO_LARGE,
        "request exceeds the grant's entire token budget",
    ),
}


def _envelope(error_code: int, message: str) -> dict[str, Any]:
    return {
        "error_code": error_code,
        "message": message,
        "request_id": request_id_var.get(),
    }


def _envelope_response(
    status_code: int,
    error_code: int,
    message: str,
    *,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    """Build the canonical JSON response with the request id echoed on a header.

    Header is set here as backup so error responses still carry the id
    even on code paths where ``RequestIDMiddleware`` cannot set it.
    """
    rid = request_id_var.get()
    response_headers = dict(headers or {})
    response_headers[REQUEST_ID_HEADER] = rid
    return JSONResponse(
        status_code=status_code,
        content=_envelope(error_code, message),
        headers=response_headers,
    )


def register_exception_handlers(app: FastAPI) -> None:
    """Attach the three envelope handlers to ``app``."""

    @app.exception_handler(StarletteHTTPException)
    async def _http_exception_handler(
        _request: Request, exc: StarletteHTTPException
    ) -> JSONResponse:
        message = exc.detail if isinstance(exc.detail, str) else str(exc.detail)
        return _envelope_response(
            exc.status_code,
            ERROR_CODE_HTTP_EXCEPTION,
            message,
            headers=dict(exc.headers or {}),
        )

    @app.exception_handler(RequestValidationError)
    async def _validation_error_handler(
        _request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        # Full validation details land in server logs; the public body
        # stays generic so user-supplied input never echoes back.
        logger.warning(f"request validation failed: {exc.errors()}")
        return _envelope_response(
            422, ERROR_CODE_VALIDATION, "request validation failed"
        )

    @app.exception_handler(SubmissionCooldownError)
    async def _submission_cooldown_handler(
        _request: Request, exc: SubmissionCooldownError
    ) -> JSONResponse:
        retry_after = max(
            1, int((exc.retry_at - datetime.now(UTC)).total_seconds()) + 1
        )
        return _envelope_response(
            429,
            ERROR_CODE_SUBMISSION_COOLDOWN,
            f"owner coldkey may submit again at {exc.retry_at.isoformat()}",
            headers={"Retry-After": str(retry_after)},
        )

    @app.exception_handler(InferenceDeclinedError)
    async def _inference_declined_handler(
        _request: Request, exc: InferenceDeclinedError
    ) -> JSONResponse:
        """Map an admission decline to its status, code, and retry hint.

        The whole mapping lives here so there is exactly one place that decides
        what a refusal looks like on the wire. See the "How a refusal is
        signalled" section of ``endpoints/inference.py`` for why the retryable
        case is the only one that moved off ``429``.
        """
        if exc.decline is InferenceDecline.AT_CAPACITY:
            return _envelope_response(
                503,
                ERROR_CODE_INFERENCE_AT_CAPACITY,
                f"{exc.lane} lane is at capacity",
                headers={"Retry-After": "1"},
            )
        terminal = _TERMINAL_DECLINE_RESPONSES.get(exc.decline)
        if terminal is not None:
            error_code, message = terminal
            return _envelope_response(429, error_code, f"{exc.lane} {message}")
        # The deliberate remainder. The message says so rather than implying the
        # platform merely failed to have an opinion: an unnamed refusal that
        # reads like an oversight invites exactly the "retry until it works"
        # behaviour that turned a spent token budget into a dead run.
        return _envelope_response(
            429,
            ERROR_CODE_INFERENCE_DECLINED,
            f"{exc.lane} grant unavailable, and the reason is deliberately not "
            "disclosed to an unauthenticated caller",
        )

    @app.exception_handler(OracleUnreachableError)
    async def _oracle_unreachable_handler(
        _request: Request, exc: OracleUnreachableError
    ) -> JSONResponse:
        logger.warning(f"pricing oracle unreachable: {exc}")
        return _envelope_response(
            503, ERROR_CODE_ORACLE_UNREACHABLE, "pricing oracle unavailable"
        )

    @app.exception_handler(PriceTooStaleError)
    async def _price_too_stale_handler(
        _request: Request, exc: PriceTooStaleError
    ) -> JSONResponse:
        logger.warning(f"pricing cache past max-stale window: {exc}")
        return _envelope_response(
            503, ERROR_CODE_PRICE_TOO_STALE, "pricing oracle unavailable"
        )

    @app.exception_handler(MalformedPriceError)
    async def _malformed_price_handler(
        _request: Request, exc: MalformedPriceError
    ) -> JSONResponse:
        logger.error(f"pricing oracle returned malformed price: {exc}")
        return _envelope_response(
            503, ERROR_CODE_MALFORMED_PRICE, "pricing data is invalid"
        )

    @app.exception_handler(PricingError)
    async def _pricing_error_handler(
        _request: Request, exc: PricingError
    ) -> JSONResponse:
        # Catch-all for any future PricingError subclass that the specific
        # handlers above don't cover.
        logger.warning(f"pricing error: {exc}")
        return _envelope_response(503, ERROR_CODE_PRICING, "pricing failure")

    @app.exception_handler(PaymentNotFoundOnChain)
    async def _payment_not_found_handler(
        _request: Request, exc: PaymentNotFoundOnChain
    ) -> JSONResponse:
        logger.info(f"payment not found on chain: {exc}")
        return _envelope_response(
            402, ERROR_CODE_PAYMENT_NOT_FOUND, "payment extrinsic not found on chain"
        )

    @app.exception_handler(PaymentExtrinsicFailed)
    async def _payment_extrinsic_failed_handler(
        _request: Request, exc: PaymentExtrinsicFailed
    ) -> JSONResponse:
        logger.info(f"payment extrinsic failed: {exc}")
        return _envelope_response(
            402,
            ERROR_CODE_PAYMENT_EXTRINSIC_FAILED,
            "payment extrinsic failed on chain",
        )

    @app.exception_handler(PaymentAmountMismatch)
    async def _payment_amount_mismatch_handler(
        _request: Request, exc: PaymentAmountMismatch
    ) -> JSONResponse:
        logger.info(f"payment amount mismatch: {exc}")
        return _envelope_response(
            402, ERROR_CODE_PAYMENT_AMOUNT_MISMATCH, "payment amount mismatch"
        )

    @app.exception_handler(PaymentRecoveryExpired)
    async def _payment_recovery_expired_handler(
        _request: Request, exc: PaymentRecoveryExpired
    ) -> JSONResponse:
        logger.info(f"payment recovery window expired: {exc}")
        return _envelope_response(
            402,
            ERROR_CODE_PAYMENT_RECOVERY_EXPIRED,
            "payment recovery window expired",
        )

    @app.exception_handler(PaymentDestinationMismatch)
    async def _payment_destination_mismatch_handler(
        _request: Request, exc: PaymentDestinationMismatch
    ) -> JSONResponse:
        logger.info(f"payment destination mismatch: {exc}")
        return _envelope_response(
            402,
            ERROR_CODE_PAYMENT_DESTINATION_MISMATCH,
            "payment destination mismatch",
        )

    @app.exception_handler(PaymentSignerMismatch)
    async def _payment_signer_mismatch_handler(
        _request: Request, exc: PaymentSignerMismatch
    ) -> JSONResponse:
        logger.info(f"payment signer mismatch: {exc}")
        return _envelope_response(
            402, ERROR_CODE_PAYMENT_SIGNER_MISMATCH, "payment signer mismatch"
        )

    @app.exception_handler(PaymentCallTypeMismatch)
    async def _payment_call_type_mismatch_handler(
        _request: Request, exc: PaymentCallTypeMismatch
    ) -> JSONResponse:
        logger.info(f"payment call type mismatch: {exc}")
        return _envelope_response(
            402,
            ERROR_CODE_PAYMENT_CALL_TYPE_MISMATCH,
            "payment call type mismatch",
        )

    @app.exception_handler(PaymentReplayedError)
    async def _payment_replayed_handler(
        _request: Request, exc: PaymentReplayedError
    ) -> JSONResponse:
        # Starlette's _lookup_exception_handler walks ``type(exc).__mro__``
        # and returns the first registered class. Registering a handler
        # on the specific subclass guarantees the MRO walk hits this one
        # before the PaymentVerifierError base; without this handler a
        # replay would fall through to the 3200 catch-all below.
        logger.info(f"payment replay rejected: {exc}")
        return _envelope_response(
            402, ERROR_CODE_PAYMENT_REPLAYED, "payment proof already used"
        )

    @app.exception_handler(PaymentVerifierError)
    async def _payment_verifier_error_handler(
        _request: Request, exc: PaymentVerifierError
    ) -> JSONResponse:
        # Catch-all for any future PaymentVerifierError subclass that the
        # specific handlers above don't cover.
        logger.warning(f"payment verifier error: {exc}")
        return _envelope_response(
            402, ERROR_CODE_PAYMENT_VERIFIER, "payment verification failed"
        )

    @app.exception_handler(AgentNotFoundError)
    async def _agent_not_found_handler(
        _request: Request, exc: AgentNotFoundError
    ) -> JSONResponse:
        logger.info(f"agent not found: {exc}")
        return _envelope_response(404, ERROR_CODE_AGENT_NOT_FOUND, "agent not found")

    @app.exception_handler(HarnessLogsDeniedError)
    async def _harness_logs_denied_handler(
        _request: Request, exc: HarnessLogsDeniedError
    ) -> JSONResponse:
        # 404 with one message for every denial. The reason is logged here and
        # never returned: telling a caller "the signature was fine but this
        # agent is not yours" turns the route into a membership oracle over
        # other miners' agent ids.
        logger.info(f"harness logs denied: {exc}")
        return _envelope_response(
            404, ERROR_CODE_HARNESS_LOGS_DENIED, "no such agent for this hotkey"
        )

    @app.exception_handler(HotkeyAgentNotFoundError)
    async def _hotkey_agent_not_found_handler(
        _request: Request, exc: HotkeyAgentNotFoundError
    ) -> JSONResponse:
        logger.info(f"no agent for hotkey: {exc}")
        return _envelope_response(
            404, ERROR_CODE_HOTKEY_AGENT_NOT_FOUND, "no agent for hotkey"
        )

    @app.exception_handler(ValidatorAuthError)
    async def _validator_auth_handler(
        _request: Request, exc: ValidatorAuthError
    ) -> JSONResponse:
        logger.info(f"validator auth rejected: {exc}")
        return _envelope_response(
            401, ERROR_CODE_VALIDATOR_AUTH, "validator authentication failed"
        )

    @app.exception_handler(AgentNotEvaluatableError)
    async def _agent_not_evaluatable_handler(
        _request: Request, exc: AgentNotEvaluatableError
    ) -> JSONResponse:
        logger.info(f"agent not in evaluatable state: {exc}")
        return _envelope_response(
            409, ERROR_CODE_AGENT_NOT_EVALUATABLE, "agent is not evaluatable"
        )

    @app.exception_handler(RetiredBenchVersionError)
    async def _retired_bench_version_handler(
        _request: Request, exc: RetiredBenchVersionError
    ) -> JSONResponse:
        logger.info(f"score rejected for a retired benchmark era: {exc}")
        return _envelope_response(
            410,
            ERROR_CODE_BENCH_VERSION_RETIRED,
            "benchmark version is retired and can no longer be scored",
        )

    @app.exception_handler(ScreenerAuthError)
    async def _screener_auth_handler(
        _request: Request, exc: ScreenerAuthError
    ) -> JSONResponse:
        logger.info(f"screener auth rejected: {exc}")
        return _envelope_response(
            401, ERROR_CODE_SCREENER_AUTH, "screener authentication failed"
        )

    @app.exception_handler(AgentNotScreenableError)
    async def _agent_not_screenable_handler(
        _request: Request, exc: AgentNotScreenableError
    ) -> JSONResponse:
        logger.info(f"agent not in screenable state: {exc}")
        return _envelope_response(
            409, ERROR_CODE_AGENT_NOT_SCREENABLE, "agent is not screenable"
        )

    @app.exception_handler(Exception)
    async def _unhandled_exception_handler(
        _request: Request, _exc: Exception
    ) -> JSONResponse:
        logger.exception("unhandled exception in request handler")
        return _envelope_response(500, ERROR_CODE_UNHANDLED, "internal server error")
