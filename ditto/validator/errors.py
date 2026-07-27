"""Typed errors for the validator worker."""

from __future__ import annotations


class ValidatorError(Exception):
    """Base class for validator-worker failures."""


class ValidatorConfigError(ValidatorError):
    """Raised when the worker is misconfigured (missing/invalid env)."""


class DittobenchError(ValidatorError):
    """Raised when a dittobench-api run fails or times out.

    ``code`` is the scorer's own machine-readable failure code when the payload
    carried one (see ``dittobench._SANDBOX_INFRASTRUCTURE_CODES``). It exists
    because that code used to die here: the worker collapsed all five sandbox
    infrastructure codes into one unlabelled ``infrastructure`` hand-back and the
    specific code survived only in a log line on the validator host. ditto-subnet#279
    read twelve dead ``mnemo*`` leases off the ticket ledger and still could not
    name what killed them at ~60 minutes, for that reason alone. Now it rides the
    wire in ``FailJobRequest.failure_detail`` and lands on the ticket.
    """

    def __init__(self, *args: object, code: str | None = None) -> None:
        super().__init__(*args)
        self.code = code


class ValidatorInfrastructureError(DittobenchError):
    """Retryable failure in validator-owned scoring infrastructure.

    Unlike an ordinary benchmark failure, this says nothing about the miner's
    artifact. The current scoring sweep must stop and let any issued lease
    expire so the submission remains eligible for another validator attempt.
    """


class LeaseDeadlineError(DittobenchError):
    """The attempt ran out of lease before the scorer returned a verdict.

    Terminal for this attempt, and deliberately **not** a
    :class:`ValidatorInfrastructureError`. A run that was given the whole lease
    and still produced nothing says something about the artifact, not about
    this host. Reporting it as ``infrastructure`` would mint a no-fault retry
    grant, apply the escalating infra backoff, and re-lease the same hanging
    artifact indefinitely — the loop that let one miner family hold three
    validator slots per round without ever reaching a verdict. It is handed
    back as ``scoring_error`` instead, so the attempt is consumed and the agent
    reaches a terminal outcome.
    """


class LeaseRevokedError(ValidatorError):
    """The platform authoritatively stopped holding the lease this run needed.

    Not a failure of the artifact, of this host, or of the run. The platform owns
    lease assignment; when its heartbeat roster stops listing a lease (an
    operator eviction, most often), continuing to score is pure waste — a full
    host slot burned for up to the lease TTL to produce a score the platform will
    refuse with a clean 409.

    Deliberately **not** a :class:`DittobenchError`, so it cannot be swallowed by
    the ``(DittobenchError, PlatformError)`` hand-back and cannot be reordered
    into or out of the :class:`LeaseDeadlineError` branch by accident. It gets
    its own handler that reports *nothing* back: there is no ticket left to hand
    back, ``scoring_error`` would consume an attempt the platform already took
    away, and ``infrastructure`` would mint a no-fault grant and re-lease the
    submission forever.
    """


class SandboxOomError(DittobenchError):
    """The miner sandbox exceeded its bounded memory allowance.

    This is an artifact-specific terminal attempt outcome, not evidence that
    the validator's shared infrastructure is unhealthy. The worker hands the
    lease back as ``sandbox_oom`` and continues with another harness without
    disabling the execution slot.
    """


class PlatformError(ValidatorError):
    """Raised when a platform ``/validator/*`` call fails."""


FAILURE_DETAIL_MAX_LENGTH = 200
"""Mirrors ``ditto.api_models.validator.FAILURE_DETAIL_MAX_LENGTH``.

Duplicated rather than imported so this module keeps its zero-dependency shape;
:func:`failure_detail` asserts nothing about it beyond truncating to it, and the
wire model is the enforcing side.
"""


def failure_detail(error: BaseException) -> str:
    """Bounded, machine-first description of why an attempt died.

    Prefers the scorer's structured code, because that is the field an operator
    can group by and the thing ditto-subnet#279 could not recover. Falls back to
    the exception type and message, which still beats the status quo of a
    three-value class and nothing else.

    Always truncates to :data:`FAILURE_DETAIL_MAX_LENGTH`. Sending an overlong
    detail would be rejected 422 by the platform and lose the whole hand-back,
    turning a diagnosis into the silent expiry the field exists to prevent -- so
    the cap is enforced here, on the sending side, not merely respected.
    """
    code = getattr(error, "code", None)
    if isinstance(code, str) and code.strip():
        return code.strip()[:FAILURE_DETAIL_MAX_LENGTH]
    message = str(error).strip()
    detail = f"{type(error).__name__}: {message}" if message else type(error).__name__
    return detail[:FAILURE_DETAIL_MAX_LENGTH]


class WeightSubmissionError(ValidatorError):
    """Raised when an on-chain weight submission fails (SDK fallback path)."""
